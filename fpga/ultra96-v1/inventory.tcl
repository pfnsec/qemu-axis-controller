puts "AXI_LAB_VIVADO_VERSION [version -short]"
puts "AXI_LAB_INSTALLED_ULTRA96_BOARDS [get_board_parts -quiet *ultra96*]"
set repositories [list]
foreach directory $argv {
    if {[file isdirectory $directory]} {
        lappend repositories [file normalize $directory]
    }
}
if {[llength $repositories] > 0} {
    set_param board.repoPaths $repositories
}
puts "AXI_LAB_REFERENCE_ULTRA96_BOARDS [get_board_parts -quiet *ultra96*]"
foreach board [get_board_parts -quiet *ultra96v1*] {
    puts "AXI_LAB_V1_BOARD $board PART [get_property PART_NAME $board]"
}
puts "AXI_LAB_ZU3EG_PARTS [get_parts -quiet xczu3eg*]"
puts "AXI_LAB_LICENSE_STATUS NOT_TESTED_SYNTHESIS_REQUIRED"
puts "AXI_LAB_HARDWARE_STATUS NOT_TESTED"
exit
