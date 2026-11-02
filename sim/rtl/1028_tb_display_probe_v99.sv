`timescale 1ns/1ps
module tb_display_probe_v99;
 reg clk=0;always #15 clk=~clk;
 reg reset=1,sof=0,request=0,empty=0,de=0;
 reg [11:0] x=0,y=0;reg [15:0] rgb=0;
 wire [191:0] record;wire toggle;
 reg [191:0] held;
 display_stream_probe probe(.clk(clk),.reset(reset),.sof(sof),.request(request),
  .empty(empty),.de(de),.x(x),.y(y),.rgb(rgb),.snapshot(record),.toggle(toggle));
 task color(input [11:0] xx,input [15:0] value);
  @(negedge clk);de=1;y=100;x=xx;rgb=value;
 endtask
 initial begin
  #60;reset=0;
  color(34,16'hf800);color(98,16'hf800);color(402,16'h07e0);color(770,16'h001f);
  @(negedge clk);de=0;request=1;empty=1;x=45;y=88;
  repeat(5)@(posedge clk);
  @(negedge clk);request=0;sof=1;
  @(posedge clk);#1;
  if(record[63:32]!=5||record[95:64]!=5)$fatal(1,"underflow accounting");
  if(record[159:96]!==64'h07e0001ff800f800)$fatal(1,"sample locations/order");
  if(record[191:160]!==32'h0058002d)$fatal(1,"request location");
  held=record;
  @(negedge clk);sof=0;request=0;empty=1;
  repeat(4)@(posedge clk);
  #1;if(record!==held)$fatal(1,"snapshot not held for CDC");
  @(negedge clk);sof=1;
  @(posedge clk);#1;
  if(record[63:32]!=0||record[95:64]!=5||record[31:0]!=1)$fatal(1,"new frame accounting");
  $display("PASS frame-latched display diagnostics: colors, positions, counts, held CDC bundle");$finish;
 end
 initial begin #5000;$fatal(1,"probe timeout");end
endmodule
