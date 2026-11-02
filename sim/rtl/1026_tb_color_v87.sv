`timescale 1ns/1ps
module tb_color;
 reg clk=0;always #5 clk=~clk;
 reg [15:0] rgb=0;wire [4:0] r,b;wire [5:0] g;
 integer k;
 ui_mixer #(.KEY_ENABLE(0)) dut(.clk(clk),.reset(1'b1),.disp_red(rgb[15:11]),
 .disp_green(rgb[10:5]),.disp_blue(rgb[4:0]),.disp_de(1'b1),.h_addr(12'd0),
 .v_addr(12'd0),.out_red(r),.out_green(g),.out_blue(b),
 .bram_en(),.bram_addr(),.bram_dout(32'd0));
 initial begin
  repeat(3)@(posedge clk);#1;
  for(k=0;k<65536;k=k+1)begin
   rgb=k;#1;if({r,g,b}!==rgb)$fatal(1,"RGB565 color changed %h",rgb);
  end
  $display("PASS all 65536 RGB565 values including magenta pass transparent OSD");$finish;
 end
endmodule
