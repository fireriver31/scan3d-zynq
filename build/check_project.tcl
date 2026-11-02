# Run on a fresh hardware copy, never the delivered source tree.
set repo [file dirname [file dirname [file normalize [info script]]]]
if {$argc != 1} {error "Usage: vivado -mode batch -source build/check_project.tcl -tclargs NEW_OUTPUT_DIR"}
set out [file normalize [lindex $argv 0]]
if {[file exists $out]} {error "Output already exists; choose a fresh directory"}
if {$out eq $repo || [string first "$repo/" "$out/"] == 0} {error "Output must be outside the repository"}
file mkdir $out
file copy [file join $repo hardware] [file join $out hardware]
cd $out
open_project [file join $out hardware vivado ov5640_ddr3_tft_hdmi.xpr]
set missing {}
foreach f [get_files -all] {if {![file exists $f]} {lappend missing $f}}
if {[llength $missing]} {error "Missing source files: $missing"}
if {[get_property PART [current_project]] ne "xc7z020clg400-1"} {error "Unexpected FPGA part"}
open_bd_design [get_files *system.bd]
validate_bd_design
report_ip_status -file [file join $out ip_status.rpt]
set f [open [file join $out project_check.txt] w]
puts $f "PROJECT_RELOCATION=PASS\nBD_VALIDATION=PASS\nSYNTHESIS_RUN=false\nBOARD_TESTED=false\nTOOL=[version -short]"
close $f
close_project
puts "PROJECT_CHECK_PASS BOARD_TESTED=false"
