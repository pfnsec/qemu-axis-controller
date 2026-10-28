set_param general.maxThreads 4
if {[llength $argv] != 1} {
    error "Expected core source directory"
}
set directory [lindex $argv 0]
foreach module {pixel dma pipeline core} {
    read_verilog -sv [file join $directory framefx_${module}.sv]
}
synth_design -top framefx_core -part xczu3eg-sbva484-1-e -mode out_of_context
create_clock -name core_clk -period 10.000 [get_ports clk]
report_utilization -file framefx-utilization.rpt
report_timing_summary -file framefx-timing.rpt
write_checkpoint -force framefx-synth.dcp
puts "FRAMEFX_SYNTHESIS_PASS xczu3eg-sbva484-1-e OUT_OF_CONTEXT_ONLY"
exit
