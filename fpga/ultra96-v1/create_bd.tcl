create_bd_design framefx
create_bd_cell -type ip -vlnv xilinx.com:ip:zynq_ultra_ps_e:3.5 ps
apply_bd_automation -rule xilinx.com:bd_rule:zynq_ultra_ps_e -config {apply_board_preset "1"} [get_bd_cells ps]
set_property -dict [list \
    CONFIG.PSU__DDRC__T_RC {64} \
    CONFIG.PSU__USE__M_AXI_GP0 {0} \
    CONFIG.PSU__USE__M_AXI_GP1 {0} \
    CONFIG.PSU__USE__M_AXI_GP2 {1} \
    CONFIG.PSU__MAXIGP2__DATA_WIDTH {32} \
    CONFIG.PSU__USE__S_AXI_GP2 {1} \
    CONFIG.PSU__SAXIGP2__DATA_WIDTH {128} \
    CONFIG.PSU__USE__IRQ0 {1} \
    CONFIG.PSU__USE__IRQ1 {0} \
    CONFIG.PSU__FPGA_PL0_ENABLE {1} \
    CONFIG.PSU__FPGA_PL1_ENABLE {0} \
    CONFIG.PSU__FPGA_PL2_ENABLE {0} \
    CONFIG.PSU__FPGA_PL3_ENABLE {0} \
    CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ {100}] [get_bd_cells ps]
create_bd_cell -type module -reference framefx_ultra96 accelerator
create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 control
create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 memory
foreach interconnect {control memory} {
    set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {1} CONFIG.NUM_CLKS {1}] [get_bd_cells $interconnect]
}
create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 reset
set_property CONFIG.C_AUX_RESET_HIGH {0} [get_bd_cells reset]
create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 zero
set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {0}] [get_bd_cells zero]
create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 one
set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {1}] [get_bd_cells one]
create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat:2.1 interrupts
set_property CONFIG.NUM_PORTS {8} [get_bd_cells interrupts]
connect_bd_intf_net [get_bd_intf_pins ps/M_AXI_HPM0_LPD] [get_bd_intf_pins control/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins control/M00_AXI] [get_bd_intf_pins accelerator/S_AXI]
connect_bd_intf_net [get_bd_intf_pins accelerator/M_AXI] [get_bd_intf_pins memory/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins memory/M00_AXI] [get_bd_intf_pins ps/S_AXI_HP0_FPD]
connect_bd_net [get_bd_pins ps/pl_clk0] [get_bd_pins ps/maxihpm0_lpd_aclk] \
    [get_bd_pins ps/saxihp0_fpd_aclk] [get_bd_pins accelerator/clk] \
    [get_bd_pins control/aclk] [get_bd_pins memory/aclk] [get_bd_pins reset/slowest_sync_clk]
connect_bd_net [get_bd_pins ps/pl_resetn0] [get_bd_pins reset/ext_reset_in]
connect_bd_net [get_bd_pins one/dout] [get_bd_pins reset/dcm_locked] [get_bd_pins reset/aux_reset_in]
connect_bd_net [get_bd_pins zero/dout] [get_bd_pins reset/mb_debug_sys_rst]
connect_bd_net [get_bd_pins reset/peripheral_aresetn] [get_bd_pins accelerator/reset_n] \
    [get_bd_pins control/aresetn] [get_bd_pins memory/aresetn]
connect_bd_net [get_bd_pins accelerator/irq] [get_bd_pins interrupts/In0]
for {set index 1} {$index < 8} {incr index} {
    connect_bd_net [get_bd_pins zero/dout] [get_bd_pins interrupts/In$index]
}
connect_bd_net [get_bd_pins interrupts/dout] [get_bd_pins ps/pl_ps_irq0]
assign_bd_address -offset 0x80000000 -range 4K -target_address_space [get_bd_addr_spaces ps/Data] \
    [get_bd_addr_segs accelerator/S_AXI/reg0] -force
assign_bd_address -offset 0x00000000 -range 2G -target_address_space [get_bd_addr_spaces accelerator/M_AXI] \
    [get_bd_addr_segs ps/SAXIGP2/HP0_DDR_LOW] -force
