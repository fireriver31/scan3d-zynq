// Frame-latched observer only. Does not gate, shift, or modify displayed pixels.
module display_stream_probe(
 input clk,input reset,input sof,input request,input empty,
 input de,input [11:0] x,y,input [15:0] rgb,
 output reg [191:0] snapshot,output reg toggle);
 reg [31:0] frames,frame_underflows,total_underflows,last_xy;
 reg [15:0] p32,p96,p400,p768;
 always @(posedge clk or posedge reset)begin
  if(reset)begin
   frames<=0;frame_underflows<=0;total_underflows<=0;last_xy<=0;
   p32<=0;p96<=0;p400<=0;p768<=0;snapshot<=0;toggle<=0;
  end else begin
   if(sof)begin
    frames<=frames+1;
    snapshot<={last_xy,p400,p768,p32,p96,total_underflows,frame_underflows,frames};
    toggle<=~toggle;frame_underflows<=0;
   end
   if(request&&empty)begin
    frame_underflows<=frame_underflows+1;total_underflows<=total_underflows+1;
    last_xy<={4'd0,y,4'd0,x};
   end
   // disp_driver's combinational coordinates lead registered camera RGB by 2.
   if(de&&y==100)case(x)
    34:p32<=rgb;98:p96<=rgb;402:p400<=rgb;770:p768<=rgb;
   endcase
  end
 end
endmodule
