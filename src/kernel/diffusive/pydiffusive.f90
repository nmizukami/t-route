module diffusive_interface

use, intrinsic :: iso_c_binding
use diffusive, only: diffnw

implicit none
contains
subroutine c_diffnw(timestep_ar_g, nts_ql_g, nts_ub_g, nts_db_g, ntss_ev_g,       &
                    nts_qtrib_g, mxncomp_g, nrch_g, z_ar_g, bo_ar_g, traps_ar_g,  & 
                    tw_ar_g, twcc_ar_g, mann_ar_g, manncc_ar_g, so_ar_g, dx_ar_g, &
                    iniq, frnw_col, frnw_g, qlat_g, ubcd_g, dbcd_g, qtrib_g,      &
                    paradim, para_ar_g, mxnbathy_g, x_bathy_g, z_bathy_g,         & 
                    mann_bathy_g, size_bathy_g, q_ev_g, elv_ev_g) bind(c)

    integer(c_int), intent(in) :: nts_ql_g, nts_ub_g, nts_db_g, nts_qtrib_g
    integer(c_int), intent(in) :: ntss_ev_g
    integer(c_int), intent(in) :: mxncomp_g, nrch_g
    integer(c_int), intent(in) :: frnw_col
    integer(c_int), intent(in) :: paradim
    integer(c_int), intent(in) :: mxnbathy_g   
    integer(c_int), dimension(nrch_g, frnw_col),    intent(in) :: frnw_g
    integer(c_int), dimension(mxncomp_g, nrch_g),   intent(in) :: size_bathy_g 
    real(c_double), dimension(nts_db_g),            intent(in) :: dbcd_g
    real(c_double), dimension(paradim),             intent(in) :: para_ar_g
    real(c_double), dimension(:),                   intent(in) :: timestep_ar_g(8)
    real(c_double), dimension(mxncomp_g, nrch_g),   intent(in) :: z_ar_g, bo_ar_g, traps_ar_g
    real(c_double), dimension(mxncomp_g, nrch_g),   intent(in) :: tw_ar_g, twcc_ar_g
    real(c_double), dimension(mxncomp_g, nrch_g),   intent(in) :: mann_ar_g, manncc_ar_g
    real(c_double), dimension(mxncomp_g, nrch_g),   intent(inout) :: so_ar_g
    real(c_double), dimension(mxncomp_g, nrch_g),   intent(in) :: dx_ar_g, iniq
    real(c_double), dimension(nts_ub_g, nrch_g),    intent(in) :: ubcd_g
    real(c_double), dimension(nts_qtrib_g, nrch_g), intent(in) :: qtrib_g
    real(c_double), dimension(nts_ql_g, mxncomp_g, nrch_g),   intent(in) :: qlat_g
    real(c_double), dimension(mxnbathy_g, mxncomp_g, nrch_g), intent(in ) :: x_bathy_g
    real(c_double), dimension(mxnbathy_g, mxncomp_g, nrch_g), intent(in ) :: z_bathy_g
    real(c_double), dimension(mxnbathy_g, mxncomp_g, nrch_g), intent(in ) :: mann_bathy_g
    real(c_double), dimension(ntss_ev_g, mxncomp_g, nrch_g),  intent(out) :: q_ev_g, elv_ev_g
    
    call diffnw(timestep_ar_g, nts_ql_g, nts_ub_g, nts_db_g, ntss_ev_g,       &
                nts_qtrib_g, mxncomp_g, nrch_g, z_ar_g, bo_ar_g, traps_ar_g,  &
                tw_ar_g, twcc_ar_g, mann_ar_g, manncc_ar_g, so_ar_g, dx_ar_g, &
                iniq, frnw_col, frnw_g, qlat_g, ubcd_g, dbcd_g, qtrib_g,      &
                paradim, para_ar_g, mxnbathy_g, x_bathy_g, z_bathy_g,         &
                mann_bathy_g, size_bathy_g, q_ev_g, elv_ev_g)
    
end subroutine c_diffnw
end module diffusive_interface