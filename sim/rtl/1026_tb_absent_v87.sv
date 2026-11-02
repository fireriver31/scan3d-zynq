`timescale 1ns/1ps
module tb_absent;
 reg clk=0;always #5 clk=~clk;reg rstn=0;
 tri1 scl,sda,ip;wire rst,online,down,sel;wire [31:0] errors;
 reg seen14=0;
 gt911_reader #(.CLK_HZ(1000000),.BUS_HZ(100000),.TIME_SCALE(100)) dut(
  .clk(clk),.rst_n(rstn),.tp_scl(scl),.tp_sda(sda),.tp_rst(rst),.tp_int(ip),
  .online(online),.down(down),.points(),.x(),.y(),.touch_seq(),.product(),
  .max_x(),.max_y(),.errors(errors),.address_sel(sel));
 always @(posedge clk)if(sel)seen14<=1;
 initial begin
  #50;rstn=1;repeat(20000)@(posedge clk);
  if(online||down||errors<2||!seen14)$fatal(1,"absent GT911 not bounded/retrying");
  $display("PASS disconnected GT911 stays offline, releases down, retries both addresses");$finish;
 end
endmodule
