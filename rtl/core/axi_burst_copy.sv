module axi_burst_copy #(
    parameter int unsigned MAX_BEATS = 16,
    parameter logic [63:0] XOR_MASK = 64'b0
) (
    input  logic        clk,
    input  logic        reset_n,
    input  logic        start,
    input  logic [63:0] source,
    input  logic [63:0] destination,
    input  logic [31:0] beats,
    input  logic [7:0]  byte_mask,
    output logic        busy,
    output logic        done,
    output logic        read_progress,
    output logic [2:0]  error_code,
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
    output logic        m_bready
);
    typedef enum logic [2:0] {IDLE, PLAN, READ_ADDRESS, READ_DATA, WRITE_DATA, WRITE_RESPONSE, FINISH} state_t;
    state_t state;
    logic [63:0] source_cursor, destination_cursor;
    logic [63:0] buffer_data [0:MAX_BEATS-1];
    logic [31:0] remaining, burst_beats, read_index, write_index, planned_beats;
    logic [7:0] strobes;
    logic address_done, read_failed;
    logic [63:0] total_bytes;

    assign total_bytes = {29'b0, beats, 3'b0};
    assign busy = state != IDLE && state != FINISH;
    assign done = state == FINISH;
    assign m_araddr = source_cursor;
    assign m_arlen = 8'(burst_beats - 1);
    assign m_arsize = 3;
    assign m_arburst = 1;
    assign m_arvalid = state == READ_ADDRESS;
    assign m_rready = state == READ_DATA;
    assign m_awaddr = destination_cursor;
    assign m_awlen = 8'(burst_beats - 1);
    assign m_awsize = 3;
    assign m_awburst = 1;
    assign m_awvalid = state == WRITE_DATA && !address_done;
    assign m_wdata = buffer_data[write_index % MAX_BEATS];
    assign m_wstrb = strobes;
    assign m_wlast = write_index + 1 == burst_beats;
    assign m_wvalid = state == WRITE_DATA && write_index < burst_beats;
    assign m_bready = state == WRITE_RESPONSE;

    always_comb begin
        planned_beats = remaining < MAX_BEATS ? remaining : MAX_BEATS;
        if (planned_beats > ((32'd4096 - {20'b0, source_cursor[11:0]}) >> 3))
            planned_beats = (32'd4096 - {20'b0, source_cursor[11:0]}) >> 3;
        if (planned_beats > ((32'd4096 - {20'b0, destination_cursor[11:0]}) >> 3))
            planned_beats = (32'd4096 - {20'b0, destination_cursor[11:0]}) >> 3;
    end

    always_ff @(posedge clk) begin
        if (!reset_n) begin
            state <= IDLE;
            source_cursor <= 0;
            destination_cursor <= 0;
            remaining <= 0;
            burst_beats <= 1;
            read_index <= 0;
            write_index <= 0;
            strobes <= 0;
            address_done <= 0;
            read_failed <= 0;
            read_progress <= 0;
            error_code <= 0;
        end else begin
            read_progress <= 0;
            case (state)
                IDLE: if (start) begin
                    source_cursor <= source;
                    destination_cursor <= destination;
                    remaining <= beats;
                    strobes <= byte_mask;
                    error_code <= 0;
                    if (beats == 0 || source[2:0] != 0 || destination[2:0] != 0 ||
                        source > (64'hffffffffffffffff - total_bytes) ||
                        destination > (64'hffffffffffffffff - total_bytes) ||
                        (source < destination + total_bytes && destination < source + total_bytes)) begin
                        error_code <= 1;
                        state <= FINISH;
                    end else state <= PLAN;
                end
                PLAN: begin
                    burst_beats <= planned_beats;
                    read_index <= 0;
                    write_index <= 0;
                    read_failed <= 0;
                    address_done <= 0;
                    state <= READ_ADDRESS;
                end
                READ_ADDRESS: if (m_arvalid && m_arready) state <= READ_DATA;
                READ_DATA: if (m_rvalid && m_rready) begin
                    read_progress <= 1;
                    buffer_data[read_index % MAX_BEATS] <= m_rdata ^ XOR_MASK;
                    read_index <= read_index + 1;
                    if (m_rresp != 0) read_failed <= 1;
                    if (m_rlast != (read_index + 1 == burst_beats)) begin
                        error_code <= 4;
                        state <= FINISH;
                    end else if (read_index + 1 == burst_beats) begin
                        if (read_failed || m_rresp != 0) begin
                            error_code <= 2;
                            state <= FINISH;
                        end else state <= WRITE_DATA;
                    end
                end
                WRITE_DATA: begin
                    if (m_awvalid && m_awready) address_done <= 1;
                    if (m_wvalid && m_wready) write_index <= write_index + 1;
                    if (address_done && write_index == burst_beats) state <= WRITE_RESPONSE;
                end
                WRITE_RESPONSE: if (m_bvalid && m_bready) begin
                    if (m_bresp != 0) begin
                        error_code <= 3;
                        state <= FINISH;
                    end else if (remaining == burst_beats) state <= FINISH;
                    else begin
                        source_cursor <= source_cursor + {29'b0, burst_beats, 3'b0};
                        destination_cursor <= destination_cursor + {29'b0, burst_beats, 3'b0};
                        remaining <= remaining - burst_beats;
                        state <= PLAN;
                    end
                end
                FINISH: state <= IDLE;
                default: state <= IDLE;
            endcase
        end
    end

`ifndef SYNTHESIS
    initial assert (MAX_BEATS >= 1 && MAX_BEATS <= 16);
    assert property (@(posedge clk) disable iff (!reset_n)
        m_arvalid && !m_arready |=> m_arvalid && $stable({m_araddr, m_arlen, m_arsize, m_arburst}));
    assert property (@(posedge clk) disable iff (!reset_n)
        m_awvalid && !m_awready |=> m_awvalid && $stable({m_awaddr, m_awlen, m_awsize, m_awburst}));
    assert property (@(posedge clk) disable iff (!reset_n)
        m_wvalid && !m_wready |=> m_wvalid && $stable({m_wdata, m_wstrb, m_wlast}));
    assert property (@(posedge clk) disable iff (!reset_n)
        state == READ_DATA |-> read_index < burst_beats && burst_beats <= MAX_BEATS);
`endif
endmodule
