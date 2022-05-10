from collections import defaultdict
from itertools import chain
from functools import partial
from tlz import concat
from joblib import delayed, Parallel
from datetime import datetime, timedelta
import time
import pandas as pd
import numpy as np
import copy

import troute.nhd_network as nhd_network
from troute.routing.fast_reach.mc_reach import compute_network_structured
import troute.routing.diffusive_utils as diff_utils
from troute.routing.fast_reach import diffusive

import logging

LOG = logging.getLogger('')

_compute_func_map = defaultdict(
    compute_network_structured,
    {
        "V02-structured": compute_network_structured,
    },
)


def _format_qlat_start_time(qlat_start_time):
    if not isinstance(qlat_start_time,datetime):
        try:
            return datetime.strptime(qlat_start_time, '%Y-%m-%d %H:%M:%S')
        except:  # TODO: make sure this doesn't introduce a silent error
            return datetime.now()

    else:
        return qlat_start_time


def _build_reach_type_list(reach_list, wbodies_segs):

    reach_type_list = [
                1 if (set(reaches) & wbodies_segs) else 0 for reaches in reach_list
            ]

    return list(zip(reach_list, reach_type_list))


def _prep_da_dataframes(
    usgs_df,
    lastobs_df,
    param_df_sub_idx,
    exclude_segments=None,
    ):
    """
    Produce, based on the segments in the param_df_sub_idx (which is a subset
    representing a subnetwork of the larger collection of all segments),
    a subset of the relevant usgs gage observation time series
    and the relevant last-valid gage observation from any
    prior model execution.
    
    exclude_segments (list): segments to exclude from param_df_sub when searching for gages
                             This catches and excludes offnetwork upstreams segments from being
                             realized as locations for DA substitution. Else, by-subnetwork
                             parallel executions fail.

    Cases to consider:
    USGS_DF, LAST_OBS
    Yes, Yes: Analysis and Assimilation; Last_Obs used to fill gaps in the front of the time series
    No, Yes: Forecasting mode;
    Yes, No; Cold-start case;
    No, No: Open-Loop;

    For both cases where USGS_DF is present, there is a sub-case where the length of the observed
    time series is as long as the simulation.

    """
    
    subnet_segs = param_df_sub_idx
    # segments in the subnetwork ONLY, no offnetwork upstreams included
    if exclude_segments:
        subnet_segs = param_df_sub_idx.difference(set(exclude_segments))
    
    # NOTE: Uncomment to easily test no observations...
    # usgs_df = pd.DataFrame()
    if not usgs_df.empty and not lastobs_df.empty:
        # index values for last obs are not correct, but line up correctly with usgs values. Switched
        lastobs_segs = (lastobs_df.index.
                        intersection(subnet_segs).
                        to_list()
                       )
        lastobs_df_sub = lastobs_df.loc[lastobs_segs]
        usgs_segs = (usgs_df.index.
                     intersection(subnet_segs).
                     reindex(lastobs_segs)[0].
                     to_list()
                    )
        da_positions_list_byseg = param_df_sub_idx.get_indexer(usgs_segs)
        usgs_df_sub = usgs_df.loc[usgs_segs]
    elif usgs_df.empty and not lastobs_df.empty:
        lastobs_segs = (lastobs_df.index.
                        intersection(subnet_segs).
                        to_list()
                       )
        lastobs_df_sub = lastobs_df.loc[lastobs_segs]
        # Create a completely empty list of gages -- the .shape[1] attribute
        # will be == 0, and that will trigger a reference to the lastobs.
        # in the compute kernel below.
        usgs_df_sub = pd.DataFrame(index=lastobs_df_sub.index,columns=[])
        usgs_segs = lastobs_segs
        da_positions_list_byseg = param_df_sub_idx.get_indexer(lastobs_segs)
    elif not usgs_df.empty and lastobs_df.empty:
        usgs_segs = list(usgs_df.index.intersection(subnet_segs))
        da_positions_list_byseg = param_df_sub_idx.get_indexer(usgs_segs)
        usgs_df_sub = usgs_df.loc[usgs_segs]
        lastobs_df_sub = pd.DataFrame(index=usgs_df_sub.index,columns=["discharge","time","model_discharge"])
    else:
        usgs_df_sub = pd.DataFrame()
        lastobs_df_sub = pd.DataFrame()
        da_positions_list_byseg = []

    return usgs_df_sub, lastobs_df_sub, da_positions_list_byseg


def _prep_da_positions_byreach(reach_list, gage_index):
    """
    produce a list of indexes of the reach_list identifying reaches with gages
    and a corresponding list of indexes of the gage_list of the gages in
    the order they are found in the reach_list.
    """
    reach_key = []
    reach_gage = []
    for i, r in enumerate(reach_list):
        for s in r:
            if s in gage_index:
                reach_key.append(i)
                reach_gage.append(s)
    gage_reach_i = gage_index.get_indexer(reach_gage)

    return reach_key, gage_reach_i

def _prep_reservoir_da_dataframes(reservoir_usgs_df, reservoir_usgs_param_df, reservoir_usace_df, reservoir_usace_param_df, waterbody_types_df_sub, t0):
    '''
    Helper function to build reservoir DA data arrays for routing computations

    Arguments
    ---------
    reservoir_usgs_df        (DataFrame): gage flow observations at USGS-type reservoirs
    reservoir_usgs_param_df  (DataFrame): USGS reservoir DA state parameters
    reservoir_usace_df       (DataFrame): gage flow observations at USACE-type reservoirs
    reservoir_usace_param_df (DataFrame): USACE reservoir DA state parameters
    waterbody_types_df_sub   (DataFrame): type-codes for waterbodies in sub domain
    t0                        (datetime): model initialization time

    Returns
    -------
    * there are many returns, because we are passing explicit arrays to mc_reach cython code
    reservoir_usgs_df_sub                 (DataFrame): gage flow observations for USGS-type reservoirs in sub domain
    reservoir_usgs_df_time                  (ndarray): time in seconds from model initialization time
    reservoir_usgs_update_time              (ndarray): update time (sec) to search for new observation at USGS reservoirs
    reservoir_usgs_prev_persisted_flow      (ndarray): previously persisted outflow rates at USGS reservoirs
    reservoir_usgs_persistence_update_time  (ndarray): update time (sec) of persisted value at USGS reservoirs
    reservoir_usgs_persistence_index        (ndarray): index denoting elapsed persistence epochs at USGS reservoirs
    reservoir_usace_df_sub                (DataFrame): gage flow observations for USACE-type reservoirs in sub domain
    reservoir_usace_df_time                 (ndarray): time in seconds from model initialization time
    reservoir_usace_update_time             (ndarray): update time (sec) to search for new observation at USACE reservoirs
    reservoir_usace_prev_persisted_flow     (ndarray): previously persisted outflow rates at USACE reservoirs
    reservoir_usace_persistence_update_time (ndarray): update time (sec) of persisted value at USACE reservoirs
    reservoir_usace_persistence_index       (ndarray): index denoting elapsed persistence epochs at USACE reservoirs

    '''
    if not reservoir_usgs_df.empty:
        usgs_wbodies_sub      = waterbody_types_df_sub[
                                    waterbody_types_df_sub['reservoir_type']==2
                                ].index
        reservoir_usgs_df_sub = reservoir_usgs_df.loc[usgs_wbodies_sub]
        reservoir_usgs_df_time = (reservoir_usgs_df.columns - t0).total_seconds().to_numpy()
        reservoir_usgs_update_time = reservoir_usgs_param_df['update_time'].loc[usgs_wbodies_sub].to_numpy()
        reservoir_usgs_prev_persisted_flow = reservoir_usgs_param_df['prev_persisted_outflow'].loc[usgs_wbodies_sub].to_numpy()
        reservoir_usgs_persistence_update_time = reservoir_usgs_param_df['persistence_update_time'].loc[usgs_wbodies_sub].to_numpy()
        reservoir_usgs_persistence_index = reservoir_usgs_param_df['persistence_index'].loc[usgs_wbodies_sub].to_numpy()
    else:
        reservoir_usgs_df_sub = pd.DataFrame()
        reservoir_usgs_df_time = pd.DataFrame().to_numpy().reshape(0,)
        reservoir_usgs_update_time = pd.DataFrame().to_numpy().reshape(0,)
        reservoir_usgs_prev_persisted_flow = pd.DataFrame().to_numpy().reshape(0,)
        reservoir_usgs_persistence_update_time = pd.DataFrame().to_numpy().reshape(0,)
        reservoir_usgs_persistence_index = pd.DataFrame().to_numpy().reshape(0,)
        if not waterbody_types_df_sub.empty:
            waterbody_types_df_sub.loc[waterbody_types_df_sub['reservoir_type'] == 2] = 1

    # select USACE reservoir DA data waterbodies in sub-domain
    if not reservoir_usace_df.empty:
        usace_wbodies_sub      = waterbody_types_df_sub[
                                    waterbody_types_df_sub['reservoir_type']==3
                                ].index
        reservoir_usace_df_sub = reservoir_usace_df.loc[usace_wbodies_sub]
        reservoir_usace_df_time = (reservoir_usace_df.columns - t0).total_seconds().to_numpy()
        reservoir_usace_df_time = (reservoir_usace_df.columns - t0).total_seconds().to_numpy()
        reservoir_usace_update_time = reservoir_usace_param_df['update_time'].loc[usace_wbodies_sub].to_numpy()
        reservoir_usace_prev_persisted_flow = reservoir_usace_param_df['prev_persisted_outflow'].loc[usace_wbodies_sub].to_numpy()
        reservoir_usace_persistence_update_time = reservoir_usace_param_df['persistence_update_time'].loc[usace_wbodies_sub].to_numpy()
        reservoir_usace_persistence_index = reservoir_usace_param_df['persistence_index'].loc[usace_wbodies_sub].to_numpy()
    else: 
        reservoir_usace_df_sub = pd.DataFrame()
        reservoir_usace_df_time = pd.DataFrame().to_numpy().reshape(0,)
        reservoir_usace_update_time = pd.DataFrame().to_numpy().reshape(0,)
        reservoir_usace_prev_persisted_flow = pd.DataFrame().to_numpy().reshape(0,)
        reservoir_usace_persistence_update_time = pd.DataFrame().to_numpy().reshape(0,)
        reservoir_usace_persistence_index = pd.DataFrame().to_numpy().reshape(0,)
        if not waterbody_types_df_sub.empty:
            waterbody_types_df_sub.loc[waterbody_types_df_sub['reservoir_type'] == 3] = 1
        
    return reservoir_usgs_df_sub, reservoir_usgs_df_time, reservoir_usgs_update_time, reservoir_usgs_prev_persisted_flow, reservoir_usgs_persistence_update_time, reservoir_usgs_persistence_index,  reservoir_usace_df_sub, reservoir_usace_df_time, reservoir_usace_update_time, reservoir_usace_prev_persisted_flow, reservoir_usace_persistence_update_time, reservoir_usace_persistence_index, waterbody_types_df_sub

def compute_nhd_routing_v02(
    connections,
    rconn,
    wbody_conn,
    reaches_bytw,
    compute_func_name,
    parallel_compute_method,
    subnetwork_target_size,
    cpu_pool,
    t0,
    dt,
    nts,
    qts_subdivisions,
    independent_networks,
    param_df,
    q0,
    qlats,
    usgs_df,
    lastobs_df,
    reservoir_usgs_df,
    reservoir_usgs_param_df,
    reservoir_usace_df,
    reservoir_usace_param_df,
    da_parameter_dict,
    assume_short_ts,
    return_courant,
    waterbodies_df,
    waterbody_parameters,
    waterbody_types_df,
    waterbody_type_specified,
    subnetwork_list,
):

    da_decay_coefficient = da_parameter_dict.get("da_decay_coefficient", 0)
    param_df["dt"] = dt
    param_df = param_df.astype("float32")
    
    start_time = time.time()
    compute_func = _compute_func_map[compute_func_name]
    if parallel_compute_method == "by-subnetwork-jit-clustered":
        
        # Create subnetwork objects if they have not already been created
        if not subnetwork_list[0] or not subnetwork_list[1]:
            networks_with_subnetworks_ordered_jit = nhd_network.build_subnetworks(
                connections, rconn, subnetwork_target_size
            )
            subnetworks_only_ordered_jit = defaultdict(dict)
            subnetworks = defaultdict(dict)
            for tw, ordered_network in networks_with_subnetworks_ordered_jit.items():
                intw = independent_networks[tw]
                for order, subnet_sets in ordered_network.items():
                    subnetworks_only_ordered_jit[order].update(subnet_sets)
                    for subn_tw, subnetwork in subnet_sets.items():
                        subnetworks[subn_tw] = {k: intw[k] for k in subnetwork}

            reaches_ordered_bysubntw = defaultdict(dict)
            for order, ordered_subn_dict in subnetworks_only_ordered_jit.items():
                for subn_tw, subnet in ordered_subn_dict.items():
                    conn_subn = {k: connections[k] for k in subnet if k in connections}
                    rconn_subn = {k: rconn[k] for k in subnet if k in rconn}

                    if not waterbodies_df.empty and not usgs_df.empty:
                        path_func = partial(
                            nhd_network.split_at_gages_waterbodies_and_junctions,
                            set(usgs_df.index.values),
                            set(waterbodies_df.index.values),
                            rconn_subn
                            )

                    elif waterbodies_df.empty and not usgs_df.empty:
                        path_func = partial(
                            nhd_network.split_at_gages_and_junctions,
                            set(usgs_df.index.values),
                            rconn_subn
                            )

                    elif not waterbodies_df.empty and usgs_df.empty:
                        path_func = partial(
                            nhd_network.split_at_waterbodies_and_junctions,
                            set(waterbodies_df.index.values),
                            rconn_subn
                            )

                    else:
                        path_func = partial(nhd_network.split_at_junction, rconn_subn)

                    reaches_ordered_bysubntw[order][
                        subn_tw
                    ] = nhd_network.dfs_decomposition(rconn_subn, path_func)

            cluster_threshold = 0.65  # When a job has a total segment count 65% of the target size, compute it
            # Otherwise, keep adding reaches.

            reaches_ordered_bysubntw_clustered = defaultdict(dict)

            for order in subnetworks_only_ordered_jit:
                cluster = 0
                reaches_ordered_bysubntw_clustered[order][cluster] = {
                    "segs": [],
                    "upstreams": {},
                    "tw": [],
                    "subn_reach_list": [],
                }
                for twi, (subn_tw, subn_reach_list) in enumerate(
                    reaches_ordered_bysubntw[order].items(), 1
                ):
                    segs = list(chain.from_iterable(subn_reach_list))
                    reaches_ordered_bysubntw_clustered[order][cluster]["segs"].extend(segs)
                    reaches_ordered_bysubntw_clustered[order][cluster]["upstreams"].update(
                        subnetworks[subn_tw]
                    )

                    reaches_ordered_bysubntw_clustered[order][cluster]["tw"].append(subn_tw)
                    reaches_ordered_bysubntw_clustered[order][cluster][
                        "subn_reach_list"
                    ].extend(subn_reach_list)

                    if (
                        len(reaches_ordered_bysubntw_clustered[order][cluster]["segs"])
                        >= cluster_threshold * subnetwork_target_size
                    ) and (
                        twi
                        < len(reaches_ordered_bysubntw[order])
                        # i.e., we haven't reached the end
                        # TODO: perhaps this should be a while condition...
                    ):
                        cluster += 1
                        reaches_ordered_bysubntw_clustered[order][cluster] = {
                            "segs": [],
                            "upstreams": {},
                            "tw": [],
                            "subn_reach_list": [],
                        }

            # save subnetworks_only_ordered_jit and reaches_ordered_bysubntw_clustered in a list
            # to be passed on to next loop. Create a deep copy of this list to prevent it from being
            # altered before being returned
            subnetwork_list = [subnetworks_only_ordered_jit, reaches_ordered_bysubntw_clustered]
            subnetwork_list = copy.deepcopy(subnetwork_list)

        else:
            subnetworks_only_ordered_jit, reaches_ordered_bysubntw_clustered = subnetwork_list
        
        if 1 == 1:
            LOG.info("JIT Preprocessing time %s seconds." % (time.time() - start_time))
            LOG.info("starting Parallel JIT calculation")
        
        start_para_time = time.time()
        # if 1 == 1:
        with Parallel(n_jobs=cpu_pool, backend="loky") as parallel:
            results_subn = defaultdict(list)
            flowveldepth_interorder = {}

            for order in range(max(subnetworks_only_ordered_jit.keys()), -1, -1):
                jobs = []
                for cluster, clustered_subns in reaches_ordered_bysubntw_clustered[
                    order
                ].items():
                    segs = clustered_subns["segs"]
                    offnetwork_upstreams = set()
                    segs_set = set(segs)
                    for seg in segs:
                        for us in rconn[seg]:
                            if us not in segs_set:
                                offnetwork_upstreams.add(us)

                    segs.extend(offnetwork_upstreams)
                    
                    common_segs = list(param_df.index.intersection(segs))
                    wbodies_segs = set(segs).symmetric_difference(common_segs)
                    
                    #Declare empty dataframe
                    waterbody_types_df_sub = pd.DataFrame()
                
                    if not waterbodies_df.empty:
                        lake_segs = list(waterbodies_df.index.intersection(segs))
                        waterbodies_df_sub = waterbodies_df.loc[
                            lake_segs,
                            [
                                "LkArea",
                                "LkMxE",
                                "OrificeA",
                                "OrificeC",
                                "OrificeE",
                                "WeirC",
                                "WeirE",
                                "WeirL",
                                "ifd",
                                "qd0",
                                "h0",
                            ],
                        ]
                        
                        #If reservoir types other than Level Pool are active
                        if not waterbody_types_df.empty:
                            waterbody_types_df_sub = waterbody_types_df.loc[
                                lake_segs,
                                [
                                    "reservoir_type",
                                ],
                            ]

                    else:
                        lake_segs = []
                        waterbodies_df_sub = pd.DataFrame()
                    
                    param_df_sub = param_df.loc[
                        common_segs,
                        ["dt", "bw", "tw", "twcc", "dx", "n", "ncc", "cs", "s0", "alt"],
                    ].sort_index()
                    
                    param_df_sub_super = param_df_sub.reindex(
                        param_df_sub.index.tolist() + lake_segs
                    ).sort_index()
                    
                    if order < max(subnetworks_only_ordered_jit.keys()):
                        for us_subn_tw in offnetwork_upstreams:
                            subn_tw_sortposition = param_df_sub_super.index.get_loc(
                                us_subn_tw
                            )
                            flowveldepth_interorder[us_subn_tw][
                                "position_index"
                            ] = subn_tw_sortposition

                    subn_reach_list = clustered_subns["subn_reach_list"]
                    upstreams = clustered_subns["upstreams"]

                    subn_reach_list_with_type = _build_reach_type_list(subn_reach_list, wbodies_segs)

                    qlat_sub = qlats.loc[param_df_sub.index]
                    q0_sub = q0.loc[param_df_sub.index]
                                        
                    param_df_sub = param_df_sub.reindex(
                        param_df_sub.index.tolist() + lake_segs
                    ).sort_index()

                    usgs_df_sub, lastobs_df_sub, da_positions_list_byseg = _prep_da_dataframes(usgs_df, lastobs_df, param_df_sub.index, offnetwork_upstreams)
                    da_positions_list_byreach, da_positions_list_bygage = _prep_da_positions_byreach(subn_reach_list, lastobs_df_sub.index)

                    qlat_sub = qlat_sub.reindex(param_df_sub.index)
                    q0_sub = q0_sub.reindex(param_df_sub.index)

                    # prepare reservoir DA data
                    (reservoir_usgs_df_sub, 
                     reservoir_usgs_df_time,
                     reservoir_usgs_update_time,
                     reservoir_usgs_prev_persisted_flow,
                     reservoir_usgs_persistence_update_time,
                     reservoir_usgs_persistence_index,
                     reservoir_usace_df_sub, 
                     reservoir_usace_df_time,
                     reservoir_usace_update_time,
                     reservoir_usace_prev_persisted_flow,
                     reservoir_usace_persistence_update_time,
                     reservoir_usace_persistence_index,
                     waterbody_types_df_sub,
                     ) = _prep_reservoir_da_dataframes(
                        reservoir_usgs_df,
                        reservoir_usgs_param_df,
                        reservoir_usace_df, 
                        reservoir_usace_param_df,
                        waterbody_types_df_sub, 
                        t0
                    )
                    
                    # results_subn[order].append(
                    #     compute_func(
                    jobs.append(
                        delayed(compute_func)(
                            nts,
                            dt,
                            qts_subdivisions,
                            subn_reach_list_with_type,
                            upstreams,
                            param_df_sub.index.values,
                            param_df_sub.columns.values,
                            param_df_sub.values,
                            q0_sub.values.astype("float32"),
                            qlat_sub.values.astype("float32"),
                            lake_segs, 
                            waterbodies_df_sub.values,
                            waterbody_parameters,
                            waterbody_types_df_sub.values.astype("int32"),
                            waterbody_type_specified,
                            t0.strftime('%Y-%m-%d_%H:%M:%S'),
                            usgs_df_sub.values.astype("float32"),
                            # flowveldepth_interorder,  # obtain keys and values from this dataset
                            np.array(da_positions_list_byseg, dtype="int32"),
                            np.array(da_positions_list_byreach, dtype="int32"),
                            np.array(da_positions_list_bygage, dtype="int32"),
                            lastobs_df_sub.get(
                                "lastobs_discharge",
                                pd.Series(index=lastobs_df_sub.index, name="Null"),
                            ).values.astype("float32"),
                            lastobs_df_sub.get(
                                "time_since_lastobs",
                                pd.Series(index=lastobs_df_sub.index, name="Null"),
                            ).values.astype("float32"),
                            da_decay_coefficient,
                            # USGS Hybrid Reservoir DA data
                            reservoir_usgs_df_sub.values.astype("float32"),
                            reservoir_usgs_df_sub.index.values.astype("int32"),
                            reservoir_usgs_df_time.astype('float32'),
                            reservoir_usgs_update_time.astype('float32'),
                            reservoir_usgs_prev_persisted_flow.astype('float32'),
                            reservoir_usgs_persistence_update_time.astype('float32'),
                            reservoir_usgs_persistence_index.astype('float32'),
                            # USACE Hybrid Reservoir DA data
                            reservoir_usace_df_sub.values.astype("float32"),
                            reservoir_usace_df_sub.index.values.astype("int32"),
                            reservoir_usace_df_time.astype('float32'),
                            reservoir_usace_update_time.astype("float32"),
                            reservoir_usace_prev_persisted_flow.astype("float32"),
                            reservoir_usace_persistence_update_time.astype("float32"),
                            reservoir_usace_persistence_index.astype("float32"),
                            {
                                us: fvd
                                for us, fvd in flowveldepth_interorder.items()
                                if us in offnetwork_upstreams
                            },
                            assume_short_ts,
                            return_courant,
                        )
                    )

                results_subn[order] = parallel(jobs)

                if order > 0:  # This is not needed for the last rank of subnetworks
                    flowveldepth_interorder = {}
                    for ci, (cluster, clustered_subns) in enumerate(
                        reaches_ordered_bysubntw_clustered[order].items()
                    ):
                        for subn_tw in clustered_subns["tw"]:
                            # TODO: This index step is necessary because we sort the segment index
                            # TODO: I think there are a number of ways we could remove the sorting step
                            #       -- the binary search could be replaced with an index based on the known topology
                            flowveldepth_interorder[subn_tw] = {}
                            subn_tw_sortposition = (
                                results_subn[order][ci][0].tolist().index(subn_tw)
                            )
                            flowveldepth_interorder[subn_tw]["results"] = results_subn[
                                order
                            ][ci][1][subn_tw_sortposition]
                            # what will it take to get just the tw FVD values into an array to pass to the next loop?
                            # There will be an empty array initialized at the top of the loop, then re-populated here.
                            # we don't have to bother with populating it after the last group

        results = []
        for order in subnetworks_only_ordered_jit:
            results.extend(results_subn[order])

        if 1 == 1:
            LOG.info("PARALLEL TIME %s seconds." % (time.time() - start_para_time))

    elif parallel_compute_method == "by-subnetwork-jit":
        # Create subnetwork objects if they have not already been created
        if not subnetwork_list[0] or not subnetwork_list[1] or not subnetwork_list[2]:
            networks_with_subnetworks_ordered_jit = nhd_network.build_subnetworks(
                connections, rconn, subnetwork_target_size
            )
            subnetworks_only_ordered_jit = defaultdict(dict)
            subnetworks = defaultdict(dict)
            for tw, ordered_network in networks_with_subnetworks_ordered_jit.items():
                intw = independent_networks[tw]
                for order, subnet_sets in ordered_network.items():
                    subnetworks_only_ordered_jit[order].update(subnet_sets)
                    for subn_tw, subnetwork in subnet_sets.items():
                        subnetworks[subn_tw] = {k: intw[k] for k in subnetwork}

            reaches_ordered_bysubntw = defaultdict(dict)
            for order, ordered_subn_dict in subnetworks_only_ordered_jit.items():
                for subn_tw, subnet in ordered_subn_dict.items():
                    conn_subn = {k: connections[k] for k in subnet if k in connections}
                    rconn_subn = {k: rconn[k] for k in subnet if k in rconn}
                    if not waterbodies_df.empty and not usgs_df.empty:
                        path_func = partial(
                            nhd_network.split_at_gages_waterbodies_and_junctions,
                            set(usgs_df.index.values),
                            set(waterbodies_df.index.values),
                            rconn_subn
                            )

                    elif waterbodies_df.empty and not usgs_df.empty:
                        path_func = partial(
                            nhd_network.split_at_gages_and_junctions,
                            set(usgs_df.index.values),
                            rconn_subn
                            )

                    elif not waterbodies_df.empty and usgs_df.empty:
                        path_func = partial(
                            nhd_network.split_at_waterbodies_and_junctions,
                            set(waterbodies_df.index.values),
                            rconn_subn
                            )

                    else:
                        path_func = partial(nhd_network.split_at_junction, rconn_subn)
                    reaches_ordered_bysubntw[order][
                        subn_tw
                    ] = nhd_network.dfs_decomposition(rconn_subn, path_func)

            # save subnetworks_only_ordered_jit and reaches_ordered_bysubntw_clustered in a list
            # to be passed on to next loop. Create a deep copy of this list to prevent it from being
            # altered before being returned
            subnetwork_list = [subnetworks_only_ordered_jit, reaches_ordered_bysubntw, subnetworks]
            subnetwork_list = copy.deepcopy(subnetwork_list)

        else:
            subnetworks_only_ordered_jit, reaches_ordered_bysubntw, subnetworks = subnetwork_list
            
        if 1 == 1:
            LOG.info("JIT Preprocessing time %s seconds." % (time.time() - start_time))
            LOG.info("starting Parallel JIT calculation")

        start_para_time = time.time()
        with Parallel(n_jobs=cpu_pool, backend="loky") as parallel:
            results_subn = defaultdict(list)
            flowveldepth_interorder = {}

            for order in range(max(subnetworks_only_ordered_jit.keys()), -1, -1):
                jobs = []
                for twi, (subn_tw, subn_reach_list) in enumerate(
                    reaches_ordered_bysubntw[order].items(), 1
                ):
                    # TODO: Confirm that a list here is best -- we are sorting,
                    # so a set might be sufficient/better
                    segs = list(chain.from_iterable(subn_reach_list))
                    offnetwork_upstreams = set()
                    segs_set = set(segs)
                    for seg in segs:
                        for us in rconn[seg]:
                            if us not in segs_set:
                                offnetwork_upstreams.add(us)

                    segs.extend(offnetwork_upstreams)
                    
                    common_segs = list(param_df.index.intersection(segs))
                    wbodies_segs = set(segs).symmetric_difference(common_segs)
                    
                    #Declare empty dataframe
                    waterbody_types_df_sub = pd.DataFrame()
                
                    if not waterbodies_df.empty:
                        lake_segs = list(waterbodies_df.index.intersection(segs))
                        waterbodies_df_sub = waterbodies_df.loc[
                            lake_segs,
                            [
                                "LkArea",
                                "LkMxE",
                                "OrificeA",
                                "OrificeC",
                                "OrificeE",
                                "WeirC",
                                "WeirE",
                                "WeirL",
                                "ifd",
                                "qd0",
                                "h0",
                            ],
                        ]
                        
                        #If reservoir types other than Level Pool are active
                        if not waterbody_types_df.empty:
                            waterbody_types_df_sub = waterbody_types_df.loc[
                                lake_segs,
                                [
                                    "reservoir_type",
                                ],
                            ]

                    else:
                        lake_segs = []
                        waterbodies_df_sub = pd.DataFrame()
                    
                    param_df_sub = param_df.loc[
                        common_segs,
                        ["dt", "bw", "tw", "twcc", "dx", "n", "ncc", "cs", "s0", "alt"],
                    ].sort_index()
                    
                    param_df_sub_super = param_df_sub.reindex(
                        param_df_sub.index.tolist() + lake_segs
                    ).sort_index()
                    
                    if order < max(subnetworks_only_ordered_jit.keys()):
                        for us_subn_tw in offnetwork_upstreams:
                            subn_tw_sortposition = param_df_sub_super.index.get_loc(
                                us_subn_tw
                            )
                            flowveldepth_interorder[us_subn_tw][
                                "position_index"
                            ] = subn_tw_sortposition

                    subn_reach_list_with_type = _build_reach_type_list(subn_reach_list, wbodies_segs)

                    qlat_sub = qlats.loc[param_df_sub.index]
                    q0_sub = q0.loc[param_df_sub.index]

                    param_df_sub = param_df_sub.reindex(
                        param_df_sub.index.tolist() + lake_segs
                    ).sort_index()

                    usgs_df_sub, lastobs_df_sub, da_positions_list_byseg = _prep_da_dataframes(usgs_df, lastobs_df, param_df_sub.index, offnetwork_upstreams)
                    da_positions_list_byreach, da_positions_list_bygage = _prep_da_positions_byreach(subn_reach_list, lastobs_df_sub.index)

                    qlat_sub = qlat_sub.reindex(param_df_sub.index)
                    q0_sub = q0_sub.reindex(param_df_sub.index)
                    
                    # prepare reservoir DA data
                    (reservoir_usgs_df_sub, 
                     reservoir_usgs_df_time,
                     reservoir_usgs_update_time,
                     reservoir_usgs_prev_persisted_flow,
                     reservoir_usgs_persistence_update_time,
                     reservoir_usgs_persistence_index,
                     reservoir_usace_df_sub, 
                     reservoir_usace_df_time,
                     reservoir_usace_update_time,
                     reservoir_usace_prev_persisted_flow,
                     reservoir_usace_persistence_update_time,
                     reservoir_usace_persistence_index,
                     waterbody_types_df_sub,
                     ) = _prep_reservoir_da_dataframes(
                        reservoir_usgs_df,
                        reservoir_usgs_param_df,
                        reservoir_usace_df, 
                        reservoir_usace_param_df,
                        waterbody_types_df_sub, 
                        t0
                    )

                    jobs.append(
                        delayed(compute_func)(
                            nts,
                            dt,
                            qts_subdivisions,
                            subn_reach_list_with_type,
                            subnetworks[subn_tw],
                            param_df_sub.index.values,
                            param_df_sub.columns.values,
                            param_df_sub.values,
                            q0_sub.values.astype("float32"),
                            qlat_sub.values.astype("float32"),
                            lake_segs,
                            waterbodies_df_sub.values,
                            waterbody_parameters,
                            waterbody_types_df_sub.values.astype("int32"),
                            waterbody_type_specified,
                            t0.strftime('%Y-%m-%d_%H:%M:%S'),
                            usgs_df_sub.values.astype("float32"),
                            # flowveldepth_interorder,  # obtain keys and values from this dataset
                            np.array(da_positions_list_byseg, dtype="int32"),
                            np.array(da_positions_list_byreach, dtype="int32"),
                            np.array(da_positions_list_bygage, dtype="int32"),
                            lastobs_df_sub.get(
                                "lastobs_discharge",
                                pd.Series(index=lastobs_df_sub.index, name="Null"),
                            ).values.astype("float32"),
                            lastobs_df_sub.get(
                                "time_since_lastobs",
                                pd.Series(index=lastobs_df_sub.index, name="Null"),
                            ).values.astype("float32"),
                            da_decay_coefficient,
                            # USGS Hybrid Reservoir DA data
                            reservoir_usgs_df_sub.values.astype("float32"),
                            reservoir_usgs_df_sub.index.values.astype("int32"),
                            reservoir_usgs_df_time.astype('float32'),
                            reservoir_usgs_update_time.astype('float32'),
                            reservoir_usgs_prev_persisted_flow.astype('float32'),
                            reservoir_usgs_persistence_update_time.astype('float32'),
                            reservoir_usgs_persistence_index.astype('float32'),
                            # USACE Hybrid Reservoir DA data
                            reservoir_usace_df_sub.values.astype("float32"),
                            reservoir_usace_df_sub.index.values.astype("int32"),
                            reservoir_usace_df_time.astype('float32'),
                            reservoir_usace_update_time.astype("float32"),
                            reservoir_usace_prev_persisted_flow.astype("float32"),
                            reservoir_usace_persistence_update_time.astype("float32"),
                            reservoir_usace_persistence_index.astype("float32"),
                            {
                                us: fvd
                                for us, fvd in flowveldepth_interorder.items()
                                if us in offnetwork_upstreams
                            },
                            assume_short_ts,
                            return_courant,
                        )
                    )

                results_subn[order] = parallel(jobs)

                if order > 0:  # This is not needed for the last rank of subnetworks
                    flowveldepth_interorder = {}
                    for twi, subn_tw in enumerate(reaches_ordered_bysubntw[order]):
                        # TODO: This index step is necessary because we sort the segment index
                        # TODO: I think there are a number of ways we could remove the sorting step
                        #       -- the binary search could be replaced with an index based on the known topology
                        flowveldepth_interorder[subn_tw] = {}
                        subn_tw_sortposition = (
                            results_subn[order][twi][0].tolist().index(subn_tw)
                        )
                        flowveldepth_interorder[subn_tw]["results"] = results_subn[
                            order
                        ][twi][1][subn_tw_sortposition]
                        # what will it take to get just the tw FVD values into an array to pass to the next loop?
                        # There will be an empty array initialized at the top of the loop, then re-populated here.
                        # we don't have to bother with populating it after the last group

        results = []
        for order in subnetworks_only_ordered_jit:
            results.extend(results_subn[order])

        if 1 == 1:
            LOG.info("PARALLEL TIME %s seconds." % (time.time() - start_para_time))

    elif parallel_compute_method == "by-network":
        with Parallel(n_jobs=cpu_pool, backend="loky") as parallel:
            jobs = []
            for twi, (tw, reach_list) in enumerate(reaches_bytw.items(), 1):
                # The X_sub lines use SEGS...
                # which is now invalid with the wbodies included.
                # So we define "common_segs" to identify regular routing segments
                # and wbodies_segs for the waterbody reaches/segments
                segs = list(chain.from_iterable(reach_list))
                common_segs = param_df.index.intersection(segs)
                # Assumes everything else is a waterbody...
                wbodies_segs = set(segs).symmetric_difference(common_segs)

                #Declare empty dataframe
                waterbody_types_df_sub = pd.DataFrame()

                # If waterbody parameters exist
                if not waterbodies_df.empty:

                    lake_segs = list(waterbodies_df.index.intersection(segs))

                    waterbodies_df_sub = waterbodies_df.loc[
                        lake_segs,
                        [
                            "LkArea",
                            "LkMxE",
                            "OrificeA",
                            "OrificeC",
                            "OrificeE",
                            "WeirC",
                            "WeirE",
                            "WeirL",
                            "ifd",
                            "qd0",
                            "h0",
                        ],
                    ]

                    #If reservoir types other than Level Pool are active
                    if not waterbody_types_df.empty:
                        waterbody_types_df_sub = waterbody_types_df.loc[
                            lake_segs,
                            [
                                "reservoir_type",
                            ],
                        ]

                else:
                    lake_segs = []
                    waterbodies_df_sub = pd.DataFrame()

                param_df_sub = param_df.loc[
                    common_segs,
                    ["dt", "bw", "tw", "twcc", "dx", "n", "ncc", "cs", "s0", "alt"],
                ].sort_index()

                reaches_list_with_type = _build_reach_type_list(reach_list, wbodies_segs)

                # qlat_sub = qlats.loc[common_segs].sort_index()
                # q0_sub = q0.loc[common_segs].sort_index()
                qlat_sub = qlats.loc[param_df_sub.index]
                q0_sub = q0.loc[param_df_sub.index]

                param_df_sub = param_df_sub.reindex(
                    param_df_sub.index.tolist() + lake_segs
                ).sort_index()

                usgs_df_sub, lastobs_df_sub, da_positions_list_byseg = _prep_da_dataframes(usgs_df, lastobs_df, param_df_sub.index)
                da_positions_list_byreach, da_positions_list_bygage = _prep_da_positions_byreach(reach_list, lastobs_df_sub.index)

                qlat_sub = qlat_sub.reindex(param_df_sub.index)
                q0_sub = q0_sub.reindex(param_df_sub.index)
                
                # prepare reservoir DA data
                (reservoir_usgs_df_sub, 
                 reservoir_usgs_df_time,
                 reservoir_usgs_update_time,
                 reservoir_usgs_prev_persisted_flow,
                 reservoir_usgs_persistence_update_time,
                 reservoir_usgs_persistence_index,
                 reservoir_usace_df_sub, 
                 reservoir_usace_df_time,
                 reservoir_usace_update_time,
                 reservoir_usace_prev_persisted_flow,
                 reservoir_usace_persistence_update_time,
                 reservoir_usace_persistence_index,
                 waterbody_types_df_sub,
                 ) = _prep_reservoir_da_dataframes(
                    reservoir_usgs_df,
                    reservoir_usgs_param_df,
                    reservoir_usace_df, 
                    reservoir_usace_param_df,
                    waterbody_types_df_sub, 
                    t0
                )

                jobs.append(
                    delayed(compute_func)(
                        nts,
                        dt,
                        qts_subdivisions,
                        reaches_list_with_type,
                        independent_networks[tw],
                        param_df_sub.index.values.astype("int64"),
                        param_df_sub.columns.values,
                        param_df_sub.values,
                        q0_sub.values.astype("float32"),
                        qlat_sub.values.astype("float32"),
                        lake_segs,
                        waterbodies_df_sub.values,
                        waterbody_parameters,
                        waterbody_types_df_sub.values.astype("int32"),
                        waterbody_type_specified,
                        t0.strftime('%Y-%m-%d_%H:%M:%S'),
                        usgs_df_sub.values.astype("float32"),
                        np.array(da_positions_list_byseg, dtype="int32"),
                        np.array(da_positions_list_byreach, dtype="int32"),
                        np.array(da_positions_list_bygage, dtype="int32"),
                        lastobs_df_sub.get("lastobs_discharge", pd.Series(index=lastobs_df_sub.index, name="Null")).values.astype("float32"),
                        lastobs_df_sub.get("time_since_lastobs", pd.Series(index=lastobs_df_sub.index, name="Null")).values.astype("float32"),
                        da_decay_coefficient,
                        # USGS Hybrid Reservoir DA data
                        reservoir_usgs_df_sub.values.astype("float32"),
                        reservoir_usgs_df_sub.index.values.astype("int32"),
                        reservoir_usgs_df_time.astype('float32'),
                        reservoir_usgs_update_time.astype('float32'),
                        reservoir_usgs_prev_persisted_flow.astype('float32'),
                        reservoir_usgs_persistence_update_time.astype('float32'),
                        reservoir_usgs_persistence_index.astype('float32'),
                        # USACE Hybrid Reservoir DA data
                        reservoir_usace_df_sub.values.astype("float32"),
                        reservoir_usace_df_sub.index.values.astype("int32"),
                        reservoir_usace_df_time.astype('float32'),
                        reservoir_usace_update_time.astype("float32"),
                        reservoir_usace_prev_persisted_flow.astype("float32"),
                        reservoir_usace_persistence_update_time.astype("float32"),
                        reservoir_usace_persistence_index.astype("float32"),
                        {},
                        assume_short_ts,
                        return_courant,
                    )
                )

            results = parallel(jobs)

    else:  # Execute in serial
        results = []
        for twi, (tw, reach_list) in enumerate(reaches_bytw.items(), 1):
            # The X_sub lines use SEGS...
            # which becomes invalid with the wbodies included.
            # So we define "common_segs" to identify regular routing segments
            # and wbodies_segs for the waterbody reaches/segments
            segs = list(chain.from_iterable(reach_list))
            common_segs = param_df.index.intersection(segs)
            # Assumes everything else is a waterbody...
            wbodies_segs = set(segs).symmetric_difference(common_segs)

            #Declare empty dataframe
            waterbody_types_df_sub = pd.DataFrame()

            # If waterbody parameters exist
            if not waterbodies_df.empty:

                lake_segs = list(waterbodies_df.index.intersection(segs))

                waterbodies_df_sub = waterbodies_df.loc[
                    lake_segs,
                    [
                        "LkArea",
                        "LkMxE",
                        "OrificeA",
                        "OrificeC",
                        "OrificeE",
                        "WeirC",
                        "WeirE",
                        "WeirL",
                        "ifd",
                        "qd0",
                        "h0",
                    ],
                ]

                #If reservoir types other than Level Pool are active
                if not waterbody_types_df.empty:
                    waterbody_types_df_sub = waterbody_types_df.loc[
                        lake_segs,
                        [
                            "reservoir_type",
                        ],
                    ]

            else:
                lake_segs = []
                waterbodies_df_sub = pd.DataFrame()

            param_df_sub = param_df.loc[
                common_segs,
                ["dt", "bw", "tw", "twcc", "dx", "n", "ncc", "cs", "s0", "alt"],
            ].sort_index()

            reaches_list_with_type = _build_reach_type_list(reach_list, wbodies_segs)

            # qlat_sub = qlats.loc[common_segs].sort_index()
            # q0_sub = q0.loc[common_segs].sort_index()
            qlat_sub = qlats.loc[param_df_sub.index]
            q0_sub = q0.loc[param_df_sub.index]

            param_df_sub = param_df_sub.reindex(
                param_df_sub.index.tolist() + lake_segs
            ).sort_index()

            usgs_df_sub, lastobs_df_sub, da_positions_list_byseg = _prep_da_dataframes(usgs_df, lastobs_df, param_df_sub.index)
            da_positions_list_byreach, da_positions_list_bygage = _prep_da_positions_byreach(reach_list, lastobs_df_sub.index)

            qlat_sub = qlat_sub.reindex(param_df_sub.index)
            q0_sub = q0_sub.reindex(param_df_sub.index)
            
            # prepare reservoir DA data
            (reservoir_usgs_df_sub, 
             reservoir_usgs_df_time,
             reservoir_usgs_update_time,
             reservoir_usgs_prev_persisted_flow,
             reservoir_usgs_persistence_update_time,
             reservoir_usgs_persistence_index,
             reservoir_usace_df_sub, 
             reservoir_usace_df_time,
             reservoir_usace_update_time,
             reservoir_usace_prev_persisted_flow,
             reservoir_usace_persistence_update_time,
             reservoir_usace_persistence_index,
             waterbody_types_df_sub,
             ) = _prep_reservoir_da_dataframes(
                reservoir_usgs_df,
                reservoir_usgs_param_df,
                reservoir_usace_df, 
                reservoir_usace_param_df,
                waterbody_types_df_sub, 
                t0
            )
            
            results.append(
                compute_func(
                    nts,
                    dt,
                    qts_subdivisions,
                    reaches_list_with_type,
                    independent_networks[tw],
                    param_df_sub.index.values.astype("int64"),
                    param_df_sub.columns.values,
                    param_df_sub.values,
                    q0_sub.values.astype("float32"),
                    qlat_sub.values.astype("float32"),
                    lake_segs,
                    waterbodies_df_sub.values,
                    waterbody_parameters,
                    waterbody_types_df_sub.values.astype("int32"),
                    waterbody_type_specified,
                    t0.strftime('%Y-%m-%d_%H:%M:%S'),
                    usgs_df_sub.values.astype("float32"),
                    np.array(da_positions_list_byseg, dtype="int32"),
                    np.array(da_positions_list_byreach, dtype="int32"),
                    np.array(da_positions_list_bygage, dtype="int32"),
                    lastobs_df_sub.get("lastobs_discharge", pd.Series(index=lastobs_df_sub.index, name="Null")).values.astype("float32"),
                    lastobs_df_sub.get("time_since_lastobs", pd.Series(index=lastobs_df_sub.index, name="Null")).values.astype("float32"),
                    da_decay_coefficient,
                    # USGS Hybrid Reservoir DA data
                    reservoir_usgs_df_sub.values.astype("float32"),
                    reservoir_usgs_df_sub.index.values.astype("int32"),
                    reservoir_usgs_df_time.astype('float32'),
                    reservoir_usgs_update_time.astype('float32'),
                    reservoir_usgs_prev_persisted_flow.astype('float32'),
                    reservoir_usgs_persistence_update_time.astype('float32'),
                    reservoir_usgs_persistence_index.astype('float32'),
                    # USACE Hybrid Reservoir DA data
                    reservoir_usace_df_sub.values.astype("float32"),
                    reservoir_usace_df_sub.index.values.astype("int32"),
                    reservoir_usace_df_time.astype('float32'),
                    reservoir_usace_update_time.astype("float32"),
                    reservoir_usace_prev_persisted_flow.astype("float32"),
                    reservoir_usace_persistence_update_time.astype("float32"),
                    reservoir_usace_persistence_index.astype("float32"),
                    {},
                    assume_short_ts,
                    return_courant,
                )
            )

    return results, subnetwork_list

def compute_diffusive_routing(
    results,
    diffusive_network_data,
    cpu_pool,
    dt,
    nts,
    q0,
    qlats,
    qts_subdivisions,
    usgs_df,
    lastobs_df,
    da_parameter_dict,
    diffusive_parameters,
    waterbodies_df,
    topobathy_data,
):

    results_diffusive = []
    for tw in diffusive_network_data: # <------- TODO - by-network parallel loop, here.

        # extract junction inflows from results array
        for j, i in enumerate(results):
            x = np.in1d(i[0], diffusive_network_data[tw]['tributary_segments'])
            if sum(x) > 0:
                if j == 0:
                    trib_segs = i[0][x]
                    trib_flow = i[1][x, ::3]
                else:
                    trib_segs = np.append(trib_segs, i[0][x])
                    trib_flow = np.append(trib_flow, i[1][x, ::3], axis = 0)  
                    
        # create DataFrame of junction inflow data            
        junction_inflows = pd.DataFrame(data = trib_flow, index = trib_segs)
        
        if not topobathy_data.empty:
            # create topobathy data for diffusive mainstem segments related to this given tw segment        
            topobathy_data_bytw  = topobathy_data.loc[diffusive_network_data[tw]['mainstem_segs']] 
        else:
            topobathy_data_bytw = pd.DataFrame()

        # build diffusive inputs
        diffusive_inputs = diff_utils.diffusive_input_data_v02(
            tw,
            diffusive_network_data[tw]['connections'],
            diffusive_network_data[tw]['rconn'],
            diffusive_network_data[tw]['reaches'],
            diffusive_network_data[tw]['mainstem_segs'],
            None, # place holder for diffusive parameters
            diffusive_network_data[tw]['param_df'],
            qlats,
            q0,
            junction_inflows,
            qts_subdivisions,
            nts,
            dt,
            waterbodies_df,
            topobathy_data_bytw,
        )
        
        # run the simulation
        out_q, out_elv = diffusive.compute_diffusive(diffusive_inputs)
        
        # unpack results
        rch_list, dat_all = diff_utils.unpack_output(
            diffusive_inputs['pynw'], 
            diffusive_inputs['ordered_reaches'], 
            out_q, 
            out_elv
        )
        
        # mask segments for which we already have MC solution
        x = np.in1d(rch_list, diffusive_network_data[tw]['tributary_segments'])
        
        results_diffusive.append(
            (
                rch_list[~x], dat_all[~x,3:], 0,
                # place-holder for streamflow DA parameters
                (np.asarray([]), np.asarray([]), np.asarray([])),
                # place-holder for reservoir DA paramters
                (np.asarray([]), np.asarray([]), np.asarray([]), np.asarray([]), np.asarray([])),
                (np.asarray([]), np.asarray([]), np.asarray([]), np.asarray([]), np.asarray([])),
                # place holder for reservoir inflows
                np.zeros(dat_all[~x,3::3].shape)
            )
        )

    return results_diffusive
