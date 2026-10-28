module framefx_dma #(
    parameter int unsigned MAX_BEATS = 16
) (
    input logic clk, reset_n,
    input logic command_valid, command_write,
    output logic command_ready,
    input logic [63:0] command_address,
    input logic [10:0] command_bytes,
    output logic done,
    output logic [2:0] error_code,
    output logic [31:0] read_increment, write_increment,
    output logic read_valid,
    output logic [63:0] read_data,
    input logic [63:0] write_data,
    input logic write_valid,
    output logic write_ready,
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
    output logic m_bready
);
    typedef enum logic [2:0] {IDLE, PLAN, READ_ADDRESS, READ_DATA, WRITE_DATA, WRITE_RESPONSE, FINISH} state_t;
    state_t state;
    logic writing, address_done;
    logic [63:0] cursor;
    logic [31:0] remaining, chunk, beat_index, planned, enabled_bytes;
    logic [2:0] read_error;

    assign command_ready = state == IDLE;
    assign done = state == FINISH;
    assign m_araddr = cursor;
    assign m_awaddr = cursor;
    assign m_arlen = 8'(chunk - 1);
    assign m_awlen = 8'(chunk - 1);
    assign m_arsize = 3;
    assign m_awsize = 3;
    assign m_arburst = 1;
    assign m_awburst = 1;
    assign m_arvalid = state == READ_ADDRESS;
    assign m_rready = state == READ_DATA;
    assign read_valid = m_rvalid && m_rready;
    assign read_data = m_rdata;
    assign m_awvalid = state == WRITE_DATA && !address_done;
    assign m_wvalid = state == WRITE_DATA && beat_index < chunk && write_valid;
    assign m_wdata = write_data;
    assign m_wlast = beat_index + 1 == chunk;
    assign write_ready = state == WRITE_DATA && beat_index < chunk && m_wready;
    assign m_bready = state == WRITE_RESPONSE;
    assign read_increment = m_arvalid && m_arready ? chunk * 8 : 0;
    assign write_increment = m_bvalid && m_bready && m_bresp == 0 ? enabled_bytes : 0;

    always_comb begin
        planned = (remaining + 7) >> 3;
        if (planned > MAX_BEATS) planned = MAX_BEATS;
        if (planned > ((32'd4096 - {20'b0, cursor[11:0]}) >> 3))
            planned = (32'd4096 - {20'b0, cursor[11:0]}) >> 3;
        m_wstrb = 0;
        for (int unsigned lane = 0; lane < 8; lane++)
            if (beat_index * 8 + lane < remaining) m_wstrb[lane] = 1;
        enabled_bytes = remaining < chunk * 8 ? remaining : chunk * 8;
    end

    always_ff @(posedge clk) begin
        if (!reset_n) begin
            state <= IDLE;
            writing <= 0;
            address_done <= 0;
            cursor <= 0;
            remaining <= 0;
            chunk <= 1;
            beat_index <= 0;
            read_error <= 0;
            error_code <= 0;
        end else case (state)
            IDLE: if (command_valid) begin
                cursor <= command_address;
                remaining <= {21'b0, command_bytes};
                writing <= command_write;
                error_code <= 0;
                state <= PLAN;
            end
            PLAN: begin
                chunk <= planned;
                beat_index <= 0;
                address_done <= 0;
                read_error <= 0;
                state <= writing ? WRITE_DATA : READ_ADDRESS;
            end
            READ_ADDRESS: if (m_arready) state <= READ_DATA;
            READ_DATA: if (m_rvalid) begin
                beat_index <= beat_index + 1;
                if (m_rresp != 0 && read_error == 0) read_error <= 2;
                if (m_rresp == 1) read_error <= 5;
                if (m_rlast != (beat_index + 1 == chunk)) read_error <= 5;
                if (beat_index + 1 == chunk) begin
                    if (read_error != 0 || m_rresp != 0 || !m_rlast) begin
                        error_code <= !m_rlast || read_error == 5 || m_rresp == 1 ? 5 : 2;
                        state <= FINISH;
                    end else if (remaining <= chunk * 8) state <= FINISH;
                    else begin
                        cursor <= cursor + {29'b0, chunk, 3'b0};
                        remaining <= remaining - chunk * 8;
                        state <= PLAN;
                    end
                end
            end
            WRITE_DATA: begin
                if (m_awvalid && m_awready) address_done <= 1;
                if (m_wvalid && m_wready) beat_index <= beat_index + 1;
                if (address_done && beat_index == chunk) state <= WRITE_RESPONSE;
            end
            WRITE_RESPONSE: if (m_bvalid) begin
                if (m_bresp != 0) begin
                    error_code <= m_bresp == 1 ? 5 : 3;
                    state <= FINISH;
                end else if (remaining <= chunk * 8) state <= FINISH;
                else begin
                    cursor <= cursor + {29'b0, chunk, 3'b0};
                    remaining <= remaining - chunk * 8;
                    state <= PLAN;
                end
            end
            FINISH: state <= IDLE;
            default: state <= IDLE;
        endcase
    end

`ifndef SYNTHESIS
    initial assert (MAX_BEATS >= 1 && MAX_BEATS <= 16);
    assert property (@(posedge clk) disable iff (!reset_n)
        command_valid && command_ready |-> command_bytes > 0 && command_bytes <= 1024 && command_address[2:0] == 0);
    assert property (@(posedge clk) disable iff (!reset_n)
        m_arvalid && !m_arready |=> m_arvalid && $stable({m_araddr, m_arlen, m_arsize, m_arburst}));
    assert property (@(posedge clk) disable iff (!reset_n)
        m_awvalid && !m_awready |=> m_awvalid && $stable({m_awaddr, m_awlen, m_awsize, m_awburst}));
    assert property (@(posedge clk) disable iff (!reset_n)
        m_wvalid && !m_wready |=> m_wvalid && $stable({m_wdata, m_wstrb, m_wlast}));
`endif
endmodule
