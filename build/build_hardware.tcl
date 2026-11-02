set script_dir [file dirname [file normalize [info script]]]
set repo [file dirname $script_dir]
if {$argc != 1} {error "Usage: vivado -mode batch -source build/build_hardware.tcl -tclargs NEW_OUTPUT_DIR"}
set out [file normalize [lindex $argv 0]]
if {[file exists $out]} {error "Output already exists; choose a fresh directory"}
if {$out eq $repo || [string first "$repo/" "$out/"] == 0} {error "Output must be outside the repository"}
file mkdir $out
file copy [file join $repo hardware] [file join $out hardware]
cd $out
set_param general.maxThreads 1
set project_dir [file join $out hardware vivado]
open_project [file join $project_dir ov5640_ddr3_tft_hdmi.xpr]
foreach f [get_files -all] {if {![file exists $f]} {error "Missing source file: $f"}}
if {[get_property PART [current_project]] ne "xc7z020clg400-1"} {error "Unexpected FPGA part"}
set resource_hook [file join $script_dir synthesis_resources.tcl]
if {![file exists $resource_hook]} {error "Missing synthesis resource hook: $resource_hook"}
add_files -fileset utils_1 -norecurse $resource_hook
set_property STEPS.SYNTH_DESIGN.TCL.PRE $resource_hook [get_runs synth_1]

generate_target all [get_ips]
open_bd_design [get_files *system.bd]
validate_bd_design
save_bd_design
generate_target all [get_files *system.bd]
set wrappers [get_files -quiet *system_wrapper.v]
if {[llength $wrappers]} {remove_files $wrappers}
add_files -norecurse [make_wrapper -files [get_files *system.bd] -top -force]
update_compile_order -fileset sources_1
reset_run synth_1
launch_runs synth_1 -jobs 1
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {error "Synthesis did not complete"}
launch_runs impl_1 -to_step write_bitstream -jobs 1
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {error "Implementation did not complete"}
open_run impl_1
report_timing_summary -report_unconstrained -file [file join $out timing.rpt]
report_utilization -file [file join $out utilization.rpt]
report_drc -file [file join $out drc.rpt]
report_cdc -details -file [file join $out cdc.rpt]
set setup [get_timing_paths -delay_type max -max_paths 1]
set hold [get_timing_paths -delay_type min -max_paths 1]
if {[llength $setup]==0 || [llength $hold]==0} {error "Missing internal timing paths"}
set wns [get_property SLACK $setup]
set whs [get_property SLACK $hold]
if {$wns < 0 || $whs < 0} {error "Internal timing failed: WNS=$wns WHS=$whs"}
if {[llength [get_drc_violations -quiet -filter {SEVERITY == "Error" || SEVERITY == "Critical Warning"}]]} {error "Severe DRC violations"}
file copy [file join $project_dir ov5640_ddr3_tft_hdmi.runs impl_1 ov5640_ddr3_tft_hdmi.bit] [file join $out scan3d.bit]
set handoff [file join $project_dir ov5640_ddr3_tft_hdmi.gen sources_1 bd system hw_handoff system.hwh]
if {![file exists $handoff]} {set handoff [file join $project_dir ov5640_ddr3_tft_hdmi.srcs sources_1 bd system hw_handoff system.hwh]}
if {![file exists $handoff]} {error "Generated HWH not found; bit is not a deployable pair"}
file copy $handoff [file join $out scan3d.hwh]
set f [open [file join $out build_status.txt] w]
puts $f "BUILD=PASS\nWNS=$wns\nWHS=$whs\nBOARD_TESTED=false\nEXTERNAL_IO_ACCEPTED=false\nTOOL=[version -short]"
close $f
close_project
puts "BUILD_PASS WNS=$wns WHS=$whs BOARD_TESTED=false"
