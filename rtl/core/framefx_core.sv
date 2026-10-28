module framefx_core #(
    parameter int unsigned MAX_BEATS = 16
) (
    input logic clk, reset_n,
    input logic [11:0] s_awaddr,
    input logic s_awvalid,
    output logic s_awready,
    input logic [31:0] s_wdata,
    input logic [3:0] s_wstrb,
    input logic s_wvalid,
    output logic s_wready,
    output logic [1:0] s_bresp,
    output logic s_bvalid,
    input logic s_bready,
    input logic [11:0] s_araddr,
    input logic s_arvalid,
    output logic s_arready,
    output logic [31:0] s_rdata,
    output logic [1:0] s_rresp,
    output logic s_rvalid,
    input logic s_rready,
    output logic [63:0] m_araddr,
    output logic [7:0] m_arlen,
    output logic [2:0] m_arsize,
    output logic [1:0] m_arburst,
    output logic m_arvalid,
    input logic m_arready,
    input logic [63:0] m_rdata,
    input logic [1:0] m_rresp,
    input logic m_rlast, m_rvalid,
    output logic m_rready,
    output logic [63:0] m_awaddr,
    output logic [7:0] m_awlen,
    output logic [2:0] m_awsize,
    output logic [1:0] m_awburst,
    output logic m_awvalid,
    input logic m_awready,
    output logic [63:0] m_wdata,
    output logic [7:0] m_wstrb,
    output logic m_wlast, m_wvalid,
    input logic m_wready,
    input logic [1:0] m_bresp,
    input logic m_bvalid,
    output logic m_bready,
    output logic irq, debug_busy
);
    logic aw_pending, w_pending, write_fire, legal_write;
    logic [11:0] write_address;
    logic [31:0] write_value;
    logic [3:0] write_strobes;
    logic [63:0] source, destination, cycles;
    logic [63:0] source_extent, destination_extent;
    logic [31:0] width, height, source_stride, destination_stride, mode, threshold;
    logic [31:0] bytes_read, bytes_written, cycles_high_snapshot;
    logic [1:0] irq_status, irq_enable, irq_clear, irq_set;
    logic busy, completed, failed, invalid_config, start_request, soft_reset, busy_start;
    logic pipeline_start, pipeline_done;
    logic [2:0] error_code, pipeline_error, dma_error;
    logic command_valid, command_write, command_ready, dma_done, read_valid, write_valid, write_ready;
    logic [63:0] command_address, read_data, write_data;
    logic [10:0] command_bytes;
    logic [31:0] read_increment, write_increment;

    assign s_awready = !aw_pending && !s_bvalid;
    assign s_wready = !w_pending && !s_bvalid;
    assign s_arready = !s_rvalid;
    assign write_fire = aw_pending && w_pending && !s_bvalid;
    assign legal_write = write_fire && write_strobes == 15 && write_address[1:0] == 0;
    assign start_request = legal_write && write_address == 12'h00c && write_value[1:0] == 1;
    assign soft_reset = legal_write && write_address == 12'h00c && write_value[1:0] == 2 && !busy;
    assign busy_start = start_request && busy;
    assign irq = |(irq_status & irq_enable);
    assign debug_busy = busy;
    assign source_extent = {32'b0, source_stride} * {32'b0, height};
    assign destination_extent = {32'b0, destination_stride} * {32'b0, height};
    assign invalid_config = width == 0 || width > 1024 || height == 0 || height > 1024 || mode > 3 ||
        (mode == 3 && (width < 3 || height < 3)) || source[5:0] != 0 || destination[5:0] != 0 ||
        source_stride[5:0] != 0 || destination_stride[5:0] != 0 ||
        {32'b0, source_stride} < (({32'b0, width} + 63) & 64'hffffffffffffffc0) ||
        {32'b0, destination_stride} < (({32'b0, width} + 63) & 64'hffffffffffffffc0) ||
        source > 64'hffffffffffffffff - source_extent || destination > 64'hffffffffffffffff - destination_extent ||
        (source < destination + destination_extent && destination < source + source_extent);

    always_comb begin
        irq_clear = legal_write && write_address == 12'h03c ? write_value[1:0] : 0;
        if ((start_request && !busy) || soft_reset) irq_clear = 3;
        irq_set = 0;
        if (pipeline_done) irq_set = pipeline_error == 0 ? 1 : 2;
        if (busy_start || (start_request && !busy && invalid_config)) irq_set[1] = 1;
    end

    framefx_pipeline pipeline (
        .clk, .reset_n, .start(pipeline_start), .source, .destination, .width(width[10:0]), .height(height[10:0]),
        .source_stride, .destination_stride, .mode(mode[1:0]), .threshold(threshold[7:0]),
        .done(pipeline_done), .error_code(pipeline_error), .command_valid, .command_write, .command_ready,
        .command_address, .command_bytes, .dma_done, .dma_error, .read_valid, .read_data,
        .write_valid, .write_data, .write_ready
    );
    framefx_dma #(.MAX_BEATS(MAX_BEATS)) dma (
        .clk, .reset_n, .command_valid, .command_write, .command_ready, .command_address, .command_bytes,
        .done(dma_done), .error_code(dma_error), .read_increment, .write_increment,
        .read_valid, .read_data, .write_valid, .write_data, .write_ready,
        .m_araddr, .m_arlen, .m_arsize, .m_arburst, .m_arvalid, .m_arready,
        .m_rdata, .m_rresp, .m_rlast, .m_rvalid, .m_rready,
        .m_awaddr, .m_awlen, .m_awsize, .m_awburst, .m_awvalid, .m_awready,
        .m_wdata, .m_wstrb, .m_wlast, .m_wvalid, .m_wready, .m_bresp, .m_bvalid, .m_bready
    );

    always_ff @(posedge clk) begin
        if (!reset_n) begin
            aw_pending <= 0; w_pending <= 0; write_address <= 0; write_value <= 0; write_strobes <= 0;
            s_bvalid <= 0; s_bresp <= 0; s_rvalid <= 0; s_rresp <= 0; s_rdata <= 0;
            source <= 0; destination <= 0; width <= 0; height <= 0;
            source_stride <= 0; destination_stride <= 0; mode <= 0; threshold <= 0;
            irq_status <= 0; irq_enable <= 0; busy <= 0; completed <= 0; failed <= 0; error_code <= 0;
            cycles <= 0; cycles_high_snapshot <= 0; bytes_read <= 0; bytes_written <= 0;
            pipeline_start <= 0;
        end else begin
            pipeline_start <= 0;
            irq_status <= (irq_status & ~irq_clear) | irq_set;
            if (busy) cycles <= cycles + 1'b1;
            bytes_read <= bytes_read + read_increment;
            bytes_written <= bytes_written + write_increment;
            if (s_bvalid && s_bready) s_bvalid <= 0;
            if (s_rvalid && s_rready) s_rvalid <= 0;
            if (s_awvalid && s_awready) begin aw_pending <= 1; write_address <= s_awaddr; end
            if (s_wvalid && s_wready) begin w_pending <= 1; write_value <= s_wdata; write_strobes <= s_wstrb; end
            if (write_fire) begin
                aw_pending <= 0; w_pending <= 0; s_bvalid <= 1; s_bresp <= 0;
                if (!legal_write) s_bresp <= 2;
                else case (write_address)
                    12'h00c: begin
                        if (write_value[1:0] == 3 || (write_value[1] && busy)) s_bresp <= 2;
                        else if (busy_start) begin failed <= 1; error_code <= 4; s_bresp <= 2; end
                        else if (start_request) begin
                            completed <= invalid_config;
                            failed <= invalid_config;
                            error_code <= invalid_config ? 1 : 0;
                            cycles <= 0; bytes_read <= 0; bytes_written <= 0; cycles_high_snapshot <= 0;
                            busy <= !invalid_config;
                            pipeline_start <= !invalid_config;
                        end else if (soft_reset) begin
                            completed <= 0; failed <= 0; error_code <= 0;
                            cycles <= 0; bytes_read <= 0; bytes_written <= 0; cycles_high_snapshot <= 0;
                        end
                    end
                    12'h014: if (busy) s_bresp <= 2; else source[31:0] <= write_value;
                    12'h018: if (busy) s_bresp <= 2; else source[63:32] <= write_value;
                    12'h01c: if (busy) s_bresp <= 2; else destination[31:0] <= write_value;
                    12'h020: if (busy) s_bresp <= 2; else destination[63:32] <= write_value;
                    12'h024: if (busy) s_bresp <= 2; else width <= write_value;
                    12'h028: if (busy) s_bresp <= 2; else height <= write_value;
                    12'h02c: if (busy) s_bresp <= 2; else source_stride <= write_value;
                    12'h030: if (busy) s_bresp <= 2; else destination_stride <= write_value;
                    12'h034: if (busy) s_bresp <= 2; else mode <= write_value;
                    12'h038: if (busy) s_bresp <= 2; else threshold <= {24'b0, write_value[7:0]};
                    12'h03c: begin end
                    12'h040: irq_enable <= write_value[1:0];
                    default: s_bresp <= 2;
                endcase
            end
            if (pipeline_done) begin
                busy <= 0; completed <= 1;
                if (pipeline_error != 0) begin failed <= 1; error_code <= pipeline_error; end
            end
            if (s_arvalid && s_arready) begin
                s_rvalid <= 1; s_rresp <= 0;
                case (s_araddr)
                    12'h000: s_rdata <= 32'h46524658;
                    12'h004: s_rdata <= 32'h00010000;
                    12'h008: s_rdata <= 32'h0000010f;
                    12'h010: s_rdata <= {29'b0, failed, completed, busy};
                    12'h014: s_rdata <= source[31:0];
                    12'h018: s_rdata <= source[63:32];
                    12'h01c: s_rdata <= destination[31:0];
                    12'h020: s_rdata <= destination[63:32];
                    12'h024: s_rdata <= width;
                    12'h028: s_rdata <= height;
                    12'h02c: s_rdata <= source_stride;
                    12'h030: s_rdata <= destination_stride;
                    12'h034: s_rdata <= mode;
                    12'h038: s_rdata <= threshold;
                    12'h03c: s_rdata <= {30'b0, irq_status};
                    12'h040: s_rdata <= {30'b0, irq_enable};
                    12'h044: s_rdata <= {29'b0, error_code};
                    12'h048: begin s_rdata <= cycles[31:0]; cycles_high_snapshot <= cycles[63:32]; end
                    12'h04c: s_rdata <= cycles_high_snapshot;
                    12'h050: s_rdata <= bytes_read;
                    12'h054: s_rdata <= bytes_written;
                    default: begin s_rdata <= 0; s_rresp <= 2; end
                endcase
            end
        end
    end

`ifndef SYNTHESIS
    assert property (@(posedge clk) disable iff (!reset_n)
        s_bvalid && !s_bready |=> s_bvalid && $stable(s_bresp));
    assert property (@(posedge clk) disable iff (!reset_n)
        s_rvalid && !s_rready |=> s_rvalid && $stable({s_rdata, s_rresp}));
    assert property (@(posedge clk) disable iff (!reset_n)
        pipeline_done |-> busy && !m_arvalid && !m_awvalid && !m_wvalid);
`endif
endmodule
