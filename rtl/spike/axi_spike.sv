module axi_spike (
    input  logic        clk,
    input  logic        reset_n,
    input  logic [11:0] s_awaddr,
    input  logic        s_awvalid,
    output logic        s_awready,
    input  logic [31:0] s_wdata,
    input  logic [3:0]  s_wstrb,
    input  logic        s_wvalid,
    output logic        s_wready,
    output logic [1:0]  s_bresp,
    output logic        s_bvalid,
    input  logic        s_bready,
    input  logic [11:0] s_araddr,
    input  logic        s_arvalid,
    output logic        s_arready,
    output logic [31:0] s_rdata,
    output logic [1:0]  s_rresp,
    output logic        s_rvalid,
    input  logic        s_rready,
    output logic [63:0] m_araddr,
    output logic [7:0]  m_arlen,
    output logic [2:0]  m_arsize,
    output logic [1:0]  m_arburst,
    output logic        m_arvalid,
    input  logic        m_arready,
    input  logic [63:0] m_rdata,
    input  logic [1:0]  m_rresp,
    input  logic        m_rlast,
    input  logic        m_rvalid,
    output logic        m_rready,
    output logic [63:0] m_awaddr,
    output logic [7:0]  m_awlen,
    output logic [2:0]  m_awsize,
    output logic [1:0]  m_awburst,
    output logic        m_awvalid,
    input  logic        m_awready,
    output logic [63:0] m_wdata,
    output logic [7:0]  m_wstrb,
    output logic        m_wlast,
    output logic        m_wvalid,
    input  logic        m_wready,
    input  logic [1:0]  m_bresp,
    input  logic        m_bvalid,
    output logic        m_bready,
    output logic        irq,
    output logic        debug_busy
);
    logic aw_pending, w_pending, deferred_response;
    logic [11:0] write_address;
    logic [31:0] write_data;
    logic [3:0] write_strobes;
    logic [63:0] source_address, destination_address;
    logic [31:0] transfer_beats;
    logic [7:0] byte_mask, delay_count;
    logic irq_status, irq_enable, error_status, job_active, dma_start, dma_done, dma_busy, read_progress;
    logic [2:0] dma_error;

    assign s_awready = !aw_pending && !s_bvalid && !deferred_response;
    assign s_wready = !w_pending && !s_bvalid && !deferred_response;
    assign s_arready = !s_rvalid;
    assign irq = irq_status && irq_enable;
    assign debug_busy = job_active || dma_busy;

    axi_burst_copy #(.XOR_MASK(64'hd3c2b1a08796e5f4)) dma (
        .clk, .reset_n, .start(dma_start), .source(source_address), .destination(destination_address),
        .beats(transfer_beats), .byte_mask, .busy(dma_busy), .done(dma_done), .read_progress, .error_code(dma_error),
        .m_araddr, .m_arlen, .m_arsize, .m_arburst, .m_arvalid, .m_arready,
        .m_rdata, .m_rresp, .m_rlast, .m_rvalid, .m_rready,
        .m_awaddr, .m_awlen, .m_awsize, .m_awburst, .m_awvalid, .m_awready,
        .m_wdata, .m_wstrb, .m_wlast, .m_wvalid, .m_wready, .m_bresp, .m_bvalid, .m_bready
    );

    always_ff @(posedge clk) begin
        if (!reset_n) begin
            job_active <= 0;
            dma_start <= 0;
            aw_pending <= 0;
            w_pending <= 0;
            deferred_response <= 0;
            write_address <= 0;
            write_data <= 0;
            write_strobes <= 0;
            source_address <= 0;
            destination_address <= 0;
            transfer_beats <= 1;
            byte_mask <= '1;
            delay_count <= 0;
            irq_status <= 0;
            irq_enable <= 0;
            error_status <= 0;
            s_bvalid <= 0;
            s_bresp <= 0;
            s_rvalid <= 0;
            s_rresp <= 0;
            s_rdata <= 0;
        end else begin
            dma_start <= 0;
            if (delay_count != 0) begin
                delay_count <= delay_count - 1'b1;
                if (delay_count == 1) dma_start <= 1;
            end
            if (deferred_response && (read_progress || dma_done)) begin
                deferred_response <= 0;
                s_bvalid <= 1;
                s_bresp <= 0;
            end
            if (dma_done) begin
                job_active <= 0;
                error_status <= dma_error != 0;
                irq_status <= 1;
            end
            if (s_bvalid && s_bready) s_bvalid <= 0;
            if (s_rvalid && s_rready) s_rvalid <= 0;
            if (s_awvalid && s_awready) begin
                aw_pending <= 1;
                write_address <= s_awaddr;
            end
            if (s_wvalid && s_wready) begin
                w_pending <= 1;
                write_data <= s_wdata;
                write_strobes <= s_wstrb;
            end
            if (aw_pending && w_pending && !s_bvalid && !deferred_response) begin
                aw_pending <= 0;
                w_pending <= 0;
                s_bvalid <= 1;
                s_bresp <= 0;
                if (write_strobes != 4'hf || write_address[1:0] != 0) begin
                    s_bresp <= 2;
                end else begin
                    case (write_address)
                        12'h008: begin
                            if (job_active || !write_data[0]) begin
                                s_bresp <= 2;
                            end else begin
                                error_status <= 0;
                                irq_status <= 0;
                                delay_count <= 200;
                                job_active <= 1;
                                deferred_response <= !write_data[1];
                                s_bvalid <= write_data[1];
                            end
                        end
                        12'h010: irq_status <= irq_status && !write_data[0];
                        12'h014: irq_enable <= write_data[0];
                        12'h018: if (!job_active) source_address[31:0] <= write_data; else s_bresp <= 2;
                        12'h01c: if (!job_active) source_address[63:32] <= write_data; else s_bresp <= 2;
                        12'h020: if (!job_active) destination_address[31:0] <= write_data; else s_bresp <= 2;
                        12'h024: if (!job_active) destination_address[63:32] <= write_data; else s_bresp <= 2;
                        12'h028: if (!job_active) byte_mask <= write_data[7:0]; else s_bresp <= 2;
                        12'h02c: if (!job_active) transfer_beats <= write_data; else s_bresp <= 2;
                        default: s_bresp <= 2;
                    endcase
                end
            end
            if (s_arvalid && s_arready) begin
                s_rvalid <= 1;
                s_rresp <= 0;
                case (s_araddr)
                    12'h000: s_rdata <= 32'h53504b31;
                    12'h004: s_rdata <= 32'h00000001;
                    12'h00c: s_rdata <= {30'b0, error_status, job_active || dma_busy};
                    12'h010: s_rdata <= {31'b0, irq_status};
                    12'h014: s_rdata <= {31'b0, irq_enable};
                    12'h02c: s_rdata <= transfer_beats;
                    12'h030: s_rdata <= {29'b0, dma_error};
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
        m_arvalid && !m_arready |=> m_arvalid && $stable({m_araddr, m_arlen, m_arsize, m_arburst}));
    assert property (@(posedge clk) disable iff (!reset_n)
        m_awvalid && !m_awready |=> m_awvalid && $stable({m_awaddr, m_awlen, m_awsize, m_awburst}));
    assert property (@(posedge clk) disable iff (!reset_n)
        m_wvalid && !m_wready |=> m_wvalid && $stable({m_wdata, m_wstrb, m_wlast}));
`endif
endmodule
