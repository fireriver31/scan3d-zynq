# Handover's expansion connector mapping. Physical jumper pin numbering remains
# unverified; do not connect an unknown 5V pin. All GT911 IO is 3.3V.
set_property -dict {PACKAGE_PIN L15 IOSTANDARD LVCMOS33 PULLUP TRUE} [get_ports tp_scl]
set_property -dict {PACKAGE_PIN K14 IOSTANDARD LVCMOS33 PULLUP TRUE} [get_ports tp_sda]
set_property -dict {PACKAGE_PIN J14 IOSTANDARD LVCMOS33} [get_ports tp_rst]
set_property -dict {PACKAGE_PIN G14 IOSTANDARD LVCMOS33 PULLUP TRUE} [get_ports tp_int]
