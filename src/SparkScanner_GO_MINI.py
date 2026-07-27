import os
import sys
import threading
import asyncio
import pygame.midi
import multiprocessing
import time

from bleak import BleakScanner, BleakClient
import tkinter as tk
from tkinter import ttk, messagebox

# Configuration
WRITE_UUID = "0000ffc1-0000-1000-8000-00805f9b34fb"
NOTIFY_HANDLE = 0x0007
WRITE_HANDLE = 0x000A
SPARK_MAC_PREFIXES = ["F7:EB:ED", "08:EB:ED"] 

class SparkMidiApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Spark MIDI Bridge July v0.0.5 (GO/MINI Fix)")
        self.root.geometry("600x450")
        self.root.configure(bg="#f5f5f5")
        
        self.spark_client = None
        self.seq = 0x20
        self.is_go_model = False 
        self.is_mini_model = False
        
        pygame.midi.init()
        self.setup_ui()
        
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.start_async_loop, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self.spark_search_loop(), self.loop)

    def build_spark_packet(self, cmd_bytes):
        """
        UNIVERSAL PACKET BUILDER.
        Uses standard 9-zero padding for BLE header.
        """
        self.seq = (self.seq + 1) & 0xFF
        
        payload = [0xf0, 0x01, self.seq] + cmd_bytes + [0xf7]
        zeros = [0x00] * 9
        
        # Total packet length: 7 header bytes (magic + length) + 9 zeros + payload length
        total_len = 16 + len(payload)
        header = [0x01, 0xfe, 0x00, 0x00, 0x53, 0xfe, total_len]
            
        return bytes(header + zeros + payload)

    def setup_ui(self):
        main = tk.Frame(self.root, bg="#f5f5f5")
        main.pack(expand=True, fill="both")
        tk.Label(main, text="SPARK MIDI BRIDGE July v0.0.5", font=("Arial", 14, "bold"), bg="#f5f5f5", fg="#333").pack(pady=10)
        
        st_frame = tk.Frame(main, bg="#f5f5f5")
        st_frame.pack(pady=5)
        self.spark_lbl = tk.Label(st_frame, text="SPARK: DISCONNECTED", fg="#c0392b", bg="#f5f5f5", font=("Arial", 9, "bold"))
        self.spark_lbl.pack()

        grid = tk.Frame(main, bg="white", relief="groove", borderwidth=1)
        grid.pack(pady=10, padx=20, fill="x")
        for i in range(0, 4):
            f = tk.Frame(grid, bg="white")
            f.pack(side="left", expand=True, pady=10)
            tk.Label(f, text=f"BTN {i}", bg="white", font=("Arial", 8, "bold")).pack()
            tk.Button(f, text="TEST", command=lambda idx=i: asyncio.run_coroutine_threadsafe(self.send_to_spark(idx), self.loop), bg="#3498db", fg="white", font=("Arial", 8)).pack(pady=5)

        self.log_box = tk.Text(main, height=12, font=("Consolas", 9), state="disabled", bg="white")
        self.log_box.pack(padx=10, pady=10, fill="both", expand=True)

    def log(self, msg): self.root.after(0, self._log, msg)
    def _log(self, msg):
        self.log_box.config(state="normal")
        self.log_box.insert("end", f"> {msg}\n"); self.log_box.see("end")
        self.log_box.config(state="disabled")

    def start_async_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def send_to_spark(self, btn_id):
        if self.spark_client and self.spark_client.is_connected:
            if self.is_go_model or self.is_mini_model:
                # For Spark GO and MINI: use command [btn_id, 0x01, 0x38, 0x00, 0x00, btn_id] and write handle 0x000A
                data = [btn_id, 0x01, 0x38, 0x00, 0x00, btn_id]
                pkt = self.build_spark_packet(data)
                mode_name = "GO" if self.is_go_model else "MINI"
                self.log(f"{mode_name} Mode: Sending Preset {btn_id+1}")
                await self.spark_client.write_gatt_char(WRITE_HANDLE, pkt, response=False)
            else:
                # For Spark 40 / Spark 2
                data = [0x15, 0x01, 0x38, 0x00, 0x00, btn_id]
                pkt = self.build_spark_packet(data)
                self.log(f"40/2 Mode: Sending Preset {btn_id+1}")
                await self.spark_client.write_gatt_char(WRITE_UUID, pkt, response=False)

    async def spark_search_loop(self):
        while True:
            if not self.spark_client or not self.spark_client.is_connected:
                try:
                    devs = await BleakScanner.discover(timeout=4.0)
                    target = next((d for d in devs if (d.name and "spark" in d.name.lower()) or 
                                  any(d.address.upper().startswith(p) for p in SPARK_MAC_PREFIXES)), None)
                    
                    if target:
                        name = target.name.lower() if target.name else ""
                        self.is_go_model = "go" in name
                        self.is_mini_model = "mini" in name
                        
                        self.log(f"Connecting to {target.name}...")
                        self.spark_client = BleakClient(target.address)
                        await self.spark_client.connect()
                        
                        model_name = "GO" if self.is_go_model else ("MINI" if self.is_mini_model else "40/2")
                        self.spark_lbl.config(text=f"SPARK {model_name}: ONLINE", fg="#27ae60")
                        
                        self.seq = 0x20
                        
                        if self.is_go_model or self.is_mini_model:
                            def notification_handler(sender, data: bytearray):
                                pass
                            try:
                                await self.spark_client.start_notify(NOTIFY_HANDLE, notification_handler)
                                self.log("Notifications started (0x0007).")
                            except Exception as ne:
                                self.log(f"Notify init error: {ne}")
                        else:
                            self.log("Sending Handshake...")
                            await self.spark_client.write_gatt_char(WRITE_UUID, self.build_spark_packet([0x00, 0x02, 0x23]), response=False)
                            await asyncio.sleep(0.1)
                            await self.spark_client.write_gatt_char(WRITE_UUID, self.build_spark_packet([0x00, 0x02, 0x10]), response=False)
                            await asyncio.sleep(0.1)
                            await self.spark_client.write_gatt_char(WRITE_UUID, self.build_spark_packet([0x00, 0x02, 0x2f]), response=False)
                        
                        self.log("Ready.")
                except Exception as e:
                    self.log(f"Conn Error: {e}")
                    self.spark_lbl.config(text="SPARK: DISCONNECTED", fg="#c0392b")
            await asyncio.sleep(5)

if __name__ == "__main__":
    multiprocessing.freeze_support()
    root = tk.Tk()
    app = SparkMidiApp(root)
    root.mainloop()
