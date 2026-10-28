set_param general.maxThreads 4
if {[llength $argv] != 1} {
    error "Expected path to toolcheck.sv"
}
read_verilog -sv [lindex $argv 0]
synth_design -top toolcheck -part xczu3eg-sbva484-1-e
report_utilization -file toolcheck-utilization.rpt
puts "AXI_LAB_SYNTHESIS_PASS xczu3eg-sbva484-1-e COUNTER_ONLY"
exit
