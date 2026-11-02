`timescale 1ns/1ps
module tb_photo_mask;
    reg clk=0;always #5 clk=~clk;
    reg rst_n=0;
    reg [11:0] awaddr=0,araddr=0;
    reg awvalid=0,wvalid=0,arvalid=0;
    wire awready,wready,arready,bvalid,rvalid;
    reg [31:0] wdata=0;
    wire [31:0] rdata;
    wire [1:0] bresp,rresp;
    reg [31:0] input_data=0;
    reg input_last=0,input_valid=0;
    reg [3:0] input_keep=15;
    wire input_ready;
    wire [31:0] output_data;
    wire [3:0] output_keep;
    wire output_valid,output_last;
    reg output_ready=0;
    reg [31:0] expected [0:24000];
    integer expected_words=0,seen_words=0,errors=0,cycle=0;
    reg was_stalled=0;
    reg [31:0] stalled_data;
    reg stalled_last;
    photo_mask_axis dut(
        .clk(clk),.rst_n(rst_n),.s_axi_awaddr(awaddr),.s_axi_awvalid(awvalid),.s_axi_awready(awready),
        .s_axi_wdata(wdata),.s_axi_wstrb(4'hf),.s_axi_wvalid(wvalid),.s_axi_wready(wready),
        .s_axi_bresp(bresp),.s_axi_bvalid(bvalid),.s_axi_bready(1'b1),
        .s_axi_araddr(araddr),.s_axi_arvalid(arvalid),.s_axi_arready(arready),
        .s_axi_rdata(rdata),.s_axi_rresp(rresp),.s_axi_rvalid(rvalid),.s_axi_rready(1'b1),
        .s_axis_tdata(input_data),.s_axis_tkeep(input_keep),.s_axis_tlast(input_last),
        .s_axis_tvalid(input_valid),.s_axis_tready(input_ready),
        .m_axis_tdata(output_data),.m_axis_tkeep(output_keep),.m_axis_tlast(output_last),
        .m_axis_tvalid(output_valid),.m_axis_tready(output_ready));
    always @(negedge clk) begin
        cycle=cycle+1;
        output_ready=(cycle%47<34) && (cycle%7!=0);
    end
    always @(posedge clk) if(rst_n) begin
        if(was_stalled && (!output_valid || output_data!==stalled_data || output_last!==stalled_last)) begin
            $display("FAIL output changed under backpressure");errors=errors+1;
        end
        was_stalled=output_valid&&!output_ready;
        stalled_data=output_data;stalled_last=output_last;
        if(output_valid&&output_ready)begin
            if(seen_words>=expected_words || output_data!==expected[seen_words] || output_keep!==15 ||
               output_last!==(seen_words==expected_words-1)) begin
                $display("FAIL packed word %0d data=%08x expected=%08x last=%b",seen_words,output_data,expected[seen_words],output_last);
                errors=errors+1;
            end
            seen_words=seen_words+1;
        end
    end else was_stalled=0;

    task write_reg(input [11:0] address,input [31:0] value);
        begin
            @(negedge clk);awaddr=address;awvalid=1;
            while(!awready)@(negedge clk);
            @(negedge clk);awvalid=0;
            repeat(2)@(negedge clk); // independent AW and W arrivals
            wdata=value;wvalid=1;
            while(!wready)@(negedge clk);
            @(negedge clk);wvalid=0;
            while(!bvalid)@(negedge clk);
            if(bresp!=0)begin $display("FAIL AXIL write response %h",address);errors=errors+1;end
            @(negedge clk);
        end
    endtask
    task read_reg(input [11:0] address,output [31:0] value);
        begin
            @(negedge clk);araddr=address;arvalid=1;
            while(!arready)@(negedge clk);
            @(negedge clk);arvalid=0;
            while(!rvalid)@(negedge clk);
            value=rdata;
            if(rresp!=0)begin $display("FAIL AXIL read response");errors=errors+1;end
            @(negedge clk);
        end
    endtask
    function [31:0] pixel(input integer index);
        begin
            case(index%3)
                0:pixel={16'hffff,16'hffff};
                1:pixel={16'hffff,16'hf800};
                2:pixel={5'd15,6'd20,5'd10,5'd18,6'd20,5'd10};
            endcase
        end
    endfunction
    function [1:0] decision(input [31:0] value);
        integer r,g,b,rr,gg,bb,score;
        begin
            r=((value>>11)&31);r=(r<<3)|(r>>2);
            g=((value>>5)&63);g=(g<<2)|(g>>4);
            b=value&31;b=(b<<3)|(b>>2);
            rr=((value>>27)&31);rr=(rr<<3)|(rr>>2);
            gg=((value>>21)&63);gg=(gg<<2)|(gg>>4);
            bb=(value>>16)&31;bb=(bb<<3)|(bb>>2);
            score=((r-rr)*(r-rr)+(g-gg)*(g-gg)+(b-bb)*(b-bb))*1024;
            decision[0]=score>655360;decision[1]=score>327680;
        end
    endfunction
    task run_frame(input integer declared_pixels,input integer sent_pixels,
                   input integer send_last,input integer expected_errors);
        integer i,watch;reg [31:0] status,value;
        begin
            expected_words=(sent_pixels+15)/16;seen_words=0;
            for(i=0;i<expected_words;i=i+1)expected[i]=0;
            // Independent fixed codes for the three declared synthetic pixels:
            // equal white=0, red/white=3, 25-code red difference=2 at these thresholds.
            for(i=0;i<sent_pixels;i=i+1)
                expected[i/16]=expected[i/16]|((i%3==0 ? 32'd0 : i%3==1 ? 32'd3 : 32'd2)<<((i%16)*2));
            write_reg(12'h00c,declared_pixels);write_reg(12'h004,1);
            read_reg(12'h008,status);
            if(!status[0])begin $display("FAIL did not start");errors=errors+1;end
            for(i=0;i<sent_pixels;i=i+1)begin
                @(negedge clk);
                input_data=pixel(i);input_last=send_last&&(i==sent_pixels-1);input_valid=1;
                @(posedge clk);
                while(!input_ready)@(posedge clk);
                @(negedge clk);input_valid=0;input_last=0;
                if(i%9==0)repeat(2)@(negedge clk);
            end
            watch=0;
            read_reg(12'h008,status);
            while(!status[1]&&watch<200)begin read_reg(12'h008,status);watch=watch+1;end
            read_reg(12'h03c,value);
            if(!status[1]||status[0]||value!==expected_errors||seen_words!=expected_words)begin
                $display("FAIL finish status=%h errors=%h want=%h output=%0d/%0d",status,value,expected_errors,seen_words,expected_words);
                errors=errors+1;
            end
            read_reg(12'h01c,value);
            if(value!=sent_pixels)begin $display("FAIL pixel count");errors=errors+1;end
        end
    endtask
    integer i;
    reg [31:0] value;
    reg [31:0] vector_input [0:383999];
    reg [31:0] vector_lut [0:767];
    reg [31:0] vector_noise [0:47];
    reg [31:0] vector_thresholds [0:1];
    string vector_path;
    initial begin
        repeat(5)@(negedge clk);rst_n=1;
        read_reg(0,value);if(value!==32'h504d1901)$fatal(1,"version");
        if ($value$plusargs("VECTORS_%s",vector_path)) begin
            $readmemh({vector_path,"/input.mem"},vector_input);
            $readmemh({vector_path,"/expected.mem"},expected,0,23999);
            $readmemh({vector_path,"/lut.mem"},vector_lut);
            $readmemh({vector_path,"/noise.mem"},vector_noise);
            $readmemh({vector_path,"/thresholds.mem"},vector_thresholds);
            expected_words=24000;seen_words=0;
            write_reg(12'h020,0);
            for(i=0;i<768;i=i+1)write_reg(12'h024,vector_lut[i]);
            write_reg(12'h028,0);
            for(i=0;i<48;i=i+1)write_reg(12'h02c,vector_noise[i]);
            write_reg(12'h010,vector_thresholds[0]);write_reg(12'h014,vector_thresholds[1]);
            write_reg(12'h00c,384000);write_reg(4,1);
            for(i=0;i<384000;i=i+1)begin
                @(negedge clk);input_data=vector_input[i];input_valid=1;input_last=(i==383999);
                @(posedge clk);while(!input_ready)@(posedge clk);
                @(negedge clk);input_valid=0;input_last=0;
            end
            repeat(100)@(negedge clk);
            read_reg(12'h03c,value);
            if(value!=0||seen_words!=24000||errors!=0)$fatal(1,"FAIL full-reference vector comparison");
            $display("PASS: all 384000 pixel decisions match independent integer reference: %s",vector_path);
            $finish;
        end
        write_reg(4,1);read_reg(12'h03c,value);
        if(value!==4)$fatal(1,"unconfigured start must fail");
        write_reg(12'h020,0);
        for(i=0;i<768;i=i+1)write_reg(12'h024,i%256);
        write_reg(12'h028,0);
        for(i=0;i<48;i=i+1)write_reg(12'h02c,1024);
        write_reg(12'h010,655360);write_reg(12'h014,327680);
        run_frame(33,33,1,0);
        run_frame(16,16,1,0);
        run_frame(33,17,1,1); // early TLAST closes partial output, reports invalid
        run_frame(16,16,0,1); // missing TLAST never blocks the output forever
        run_frame(1,1,1,0);
        run_frame(384000,384000,1,0); // full 800x480, with input and output stalls
        write_reg(12'h00c,0);write_reg(4,1);read_reg(12'h03c,value);
        if(value!==16)$fatal(1,"zero pixels must fail");
        if(errors!=0)$fatal(1,"FAIL errors=%0d",errors);
        $display("PASS: AXIL independent channels, unconfigured/zero rejection, RGB reference arithmetic,");
        $display("      strong/possible packing, full-frame count, partial words, early/missing TLAST, backpressure");
        $finish;
    end
    initial begin #100000000;$fatal(1,"simulation watchdog");end
endmodule
