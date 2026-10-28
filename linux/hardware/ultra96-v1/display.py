#!/usr/bin/env python3
import argparse
import json
import math
from pathlib import Path


def transform(source, width, height, mode, threshold=127):
    if (not 1 <= width <= 1024 or not 1 <= height <= 1024 or len(source) != width * height
            or mode not in range(4) or not 0 <= threshold <= 255 or (mode == 3 and min(width, height) < 3)):
        raise ValueError("Invalid reference geometry/mode")
    if mode == 0:
        return bytes(source)
    if mode == 1:
        return bytes(255 - value for value in source)
    if mode == 2:
        return bytes(255 if value >= threshold else 0 for value in source)
    output = bytearray(len(source))
    for row in range(1, height - 1):
        for column in range(1, width - 1):
            offset = row * width + column
            top, bottom = offset - width, offset + width
            horizontal = (-source[top - 1] + source[top + 1] - 2 * source[offset - 1]
                          + 2 * source[offset + 1] - source[bottom - 1] + source[bottom + 1])
            vertical = (-source[top - 1] - 2 * source[top] - source[top + 1]
                        + source[bottom - 1] + 2 * source[bottom] + source[bottom + 1])
            output[offset] = min(255, (abs(horizontal) + abs(vertical)) // 4)
    return bytes(output)


def pattern(width, height, frame):
    center = width // 2 + round(width * 0.18 * math.sin(frame / 18))
    pixels = bytearray(width * height)
    for row in range(height):
        for column in range(width):
            radius = (column - center)**2 + (row - height // 2)**2
            pixel = 42 if ((column // 32) + (row // 32)) % 2 else 78
            if 22 < column < 95 and 25 < row < height - 25:
                pixel = row * 255 // max(1, height - 1)
            if radius < (height * 0.27)**2:
                pixel = 220
            if radius < (height * 0.15)**2:
                pixel = 25
            if column > width - 60 and row % 30 < 15:
                pixel = 245
            pixels[row * width + column] = pixel
    return bytes(pixels)


def main():
    import tkinter as tkinter

    parser = argparse.ArgumentParser(description="H0 CPU reference display; no PL, DMA or MMIO access")
    parser.add_argument("--windowed", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    arguments = parser.parse_args()
    window = tkinter.Tk()
    window.title("axi-cosim-lab | Ultra96 V1 | CPU reference, NOT FPGA")
    window.configure(background="#101722")
    width, height = 320, 240
    scale = 2 if window.winfo_screenwidth() >= 1400 and window.winfo_screenheight() >= 950 else 1
    if not arguments.windowed:
        window.after(500, lambda: window.attributes("-fullscreen", True))
    content = tkinter.Frame(window, background="#101722", padx=24, pady=20)
    content.pack(expand=True)

    def label(parent, text, size=12, color="#e9f1fb"):
        widget = tkinter.Label(parent, text=text, font=("sans", size), foreground=color, background="#101722", justify="left")
        widget.pack(anchor="w", pady=4)
        return widget

    label(content, "AXI-COSIM-LAB / H0 BOOT + DISPLAY", 12, "#5fe2c6")
    label(content, "Hello, Ultra96 V1.", 28)
    label(content, "CPU reference visualization - NOT FPGA acceleration", 15, "#ffdb9c")
    label(content, "Vendor boot firmware preserved. Framefx PL, physical DMA and IRQ are NOT TESTED.", 10, "#ffdb9c")
    panels = tkinter.Frame(content, background="#101722")
    panels.pack(pady=14)
    images = []
    titles = []
    for title in ("GENERATED INPUT", "SOBEL / CPU REFERENCE"):
        panel = tkinter.Frame(panels, background="#101722", padx=7)
        panel.pack(side="left")
        titles.append(label(panel, title))
        images.append(label(panel, ""))
    controls = tkinter.Frame(content, background="#101722")
    controls.pack(anchor="w")
    state = dict(mode=3, frame=0, paused=False, image_handles=[])
    names = ("Copy", "Invert", "Threshold", "Sobel")
    buttons = []
    counter = label(content, "Starting CPU reference...")
    diagnostics = label(content, "Board diagnostics pending; operator must confirm visible DisplayPort output.", 10, "#a5b6cc")
    label(content, "1-4: filter | Space: pause | F11: fullscreen | Esc: windowed\nEvidence: /var/lib/axi-lab/boot-report.json | Next: H1 registers -> H2 DMA/IRQ -> H3 PL filters", 10, "#a5b6cc")

    def render():
        source = pattern(width, height, state["frame"])
        output = transform(source, width, height, state["mode"])
        handles = []
        for widget, pixels in zip(images, (source, output)):
            photo = tkinter.PhotoImage(data=f"P5\n{width} {height}\n255\n".encode() + pixels, format="PPM")
            photo = photo.zoom(scale)
            widget.configure(image=photo)
            handles.append(photo)
        state["image_handles"] = handles
        titles[1].configure(text=names[state["mode"]].upper() + " / CPU REFERENCE")
        counter.configure(text=f"Frame {state['frame']} | 320 x 240 | CPU only" + (" | PAUSED" if state["paused"] else ""))
        for mode, button in enumerate(buttons):
            button.configure(background="#4cddbd" if mode == state["mode"] else "#25354b",
                             foreground="#101722" if mode == state["mode"] else "#e9f1fb")

    def select(mode):
        state["mode"] = mode
        render()

    def pause():
        state["paused"] = not state["paused"]
        render()

    for mode, name in enumerate(names):
        button = tkinter.Button(controls, text=f"{mode + 1} - {name}", command=lambda selected=mode: select(selected), padx=14, pady=7)
        button.pack(side="left", padx=4)
        buttons.append(button)
        window.bind(str(mode + 1), lambda event, selected=mode: select(selected))
    tkinter.Button(controls, text="Pause / resume", command=pause, padx=14, pady=7).pack(side="left", padx=4)
    window.bind("<space>", lambda event: pause())
    window.bind("<F11>", lambda event: window.attributes("-fullscreen", not window.attributes("-fullscreen")))
    window.bind("<Escape>", lambda event: window.attributes("-fullscreen", False))

    def tick():
        if not state["paused"]:
            state["frame"] += 1
            render()
        window.after(150, tick)

    def refresh_diagnostics():
        try:
            report = json.loads(Path("/var/lib/axi-lab/boot-report.json").read_text())
            connectors = "; ".join(item["name"] + ": " + item["status"] for item in report["connectors"])
            diagnostics.configure(text=f"{report['model']} / {report['board_identity']} | Kernel {report['kernel']}\n"
                                  + (connectors or "No DRM connector observed in boot snapshot; operator check required."))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        window.after(3000, refresh_diagnostics)

    def smoke_test():
        assert window.winfo_height() >= content.winfo_reqheight(), "Display content is vertically clipped"
        for mode in range(4):
            select(mode)
            window.update_idletasks()
            assert len(state["image_handles"]) == 2
            assert state["image_handles"][1].width() == width * scale
        pause()
        assert state["paused"]
        pause()
        assert not state["paused"]
        print("H0_GUI_SMOKE_PASS: Tk rendering and four CPU modes; NOT physical display evidence", flush=True)
        window.destroy()

    render()
    refresh_diagnostics()
    window.after(150, tick)
    if arguments.smoke_test:
        window.after(1000, smoke_test)
    print("H0_CPU_DISPLAY_STARTED; operator confirmation required, framefx PL NOT TESTED", flush=True)
    window.mainloop()


if __name__ == "__main__":
    main()
