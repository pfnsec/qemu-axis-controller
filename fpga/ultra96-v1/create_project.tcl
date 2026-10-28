set_param general.maxThreads 4
if {[llength $argv] != 2} {
    error "Expected repository root and build stage (validate or bitstream)"
}
set root [file normalize [lindex $argv 0]]
set stage [lindex $argv 1]
if {$stage ni {validate bitstream}} { error "Invalid build stage" }
if {[version -short] ne "2026.1"} { error "Reviewed Vivado version is 2026.1" }
set_param board.repoPaths [list [file join $root third_party avnet-bdf-ultra96v1]]
create_project framefx [file join [pwd] project] -part xczu3eg-sbva484-1-e -force
set_property board_part em.avnet.com:ultra96v1:part0:1.2 [current_project]
set_property target_language Verilog [current_project]
set_property simulator_language Mixed [current_project]
foreach module {pixel dma pipeline core} {
    add_files [file join $root rtl core framefx_${module}.sv]
}
add_files [file join $root rtl board framefx_ultra96.sv]
source [file join $root fpga ultra96-v1 create_bd.tcl]
validate_bd_design
foreach property {CONFIG.C_EXT_RESET_HIGH CONFIG.C_AUX_RESET_HIGH} {
    if {[get_property $property [get_bd_cells reset]] != 0} { error "Unexpected reset polarity" }
}
save_bd_design
write_bd_tcl -force [file join [pwd] generated-bd.tcl]
report_ip_status -file [file join [pwd] ip-status.rpt]
set parameter_file [open [file join [pwd] ps-properties.txt] w]
foreach property [lsort [list_property [get_bd_cells ps]]] {
    if {[string match CONFIG.PSU* $property]} {
        puts $parameter_file "$property=[get_property $property [get_bd_cells ps]]"
    }
}
close $parameter_file
set address_file [open [file join [pwd] address-map.txt] w]
foreach segment [get_bd_addr_segs -hierarchical] {
    puts $address_file "$segment OFFSET=[get_property OFFSET $segment] RANGE=[get_property RANGE $segment]"
}
close $address_file
generate_target all [get_files framefx.bd]
make_wrapper -files [get_files framefx.bd] -top
add_files [file join [pwd] project framefx.gen sources_1 bd framefx hdl framefx_wrapper.v]
set_property top framefx_wrapper [current_fileset]
update_compile_order -fileset sources_1
write_hw_platform -fixed -force [file join [pwd] framefx.xsa]
puts "FRAMEFX_BD_VALIDATED xczu3eg-sbva484-1-e"
if {$stage eq "validate"} { exit }
set_property synth_checkpoint_mode None [get_files framefx.bd]
launch_runs synth_1 -jobs 4
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} { error "Synthesis failed" }
launch_runs impl_1 -to_step route_design -jobs 4
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} { error "Implementation failed" }
open_run impl_1
report_timing_summary -delay_type min_max -report_unconstrained -file timing.rpt
report_utilization -file utilization.rpt
report_drc -file drc.rpt
report_route_status -file route-status.rpt
check_timing -verbose -file check-timing.rpt
set timing_file [open check-timing.rpt r]
set timing_checks [read $timing_file]
close $timing_file
foreach category {no_clock unconstrained_internal_endpoints no_input_delay no_output_delay multiple_clock loops latch_loops} {
    if {![regexp "checking $category \\(0\\)" $timing_checks]} {
        error "Unresolved timing coverage check: $category"
    }
}
set setup [get_timing_paths -delay_type max -max_paths 1]
set hold [get_timing_paths -delay_type min -max_paths 1]
if {[llength $setup] != 1 || [llength $hold] != 1} { error "Missing timing paths" }
if {[get_property SLACK $setup] < 0 || [get_property SLACK $hold] < 0} { error "Routed timing failed" }
if {[llength [get_drc_violations -quiet -filter {SEVERITY == Error || SEVERITY == {Critical Warning}}]]} {
    error "Unresolved critical design-rule violation"
}
write_checkpoint -force framefx-routed.dcp
write_bitstream -force framefx.bit
write_hw_platform -fixed -force framefx.xsa
puts "FRAMEFX_BITSTREAM_BUILT xczu3eg-sbva484-1-e HARDWARE_NOT_TESTED"
exit
