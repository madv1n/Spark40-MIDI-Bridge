import os
import sys
import json
import threading
import asyncio
import pygame.midi
import multiprocessing
import time
import webbrowser

# Windows-specific imports
try:
    import winrt.windows.devices.midi as midi
    import winrt.windows.devices.enumeration as enumeration
    from winrt.windows.storage.streams import DataReader
    IS_WINDOWS = True
except ImportError:
    IS_WINDOWS = False

from bleak import BleakScanner, BleakClient
import tkinter as tk
from tkinter import ttk, messagebox

# Configuration
CONFIG_FILE = "spark_config.json"
WRITE_UUID = "0000ffc1-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ffc2-0000-1000-8000-00805f9b34fb"
SPARK_MAC_PREFIXES = ["F7:EB:ED", "08:EB:ED"]

class SparkMidiApp:
    def __init__(self, root):
        self.root = root
        self.app_version = "v1.2.0"
        self.root.title(f"Spark MIDI Bridge {self.app_version}")
        self.root.geometry("620x450")
        self.root.minsize(520, 370)
        self.root.configure(bg="#f5f5f5")
        
        if os.path.exists("icon.ico"):
            try: self.root.iconbitmap("icon.ico")
            except: pass
            
        self.midi_device_var = tk.StringVar(value="Auto-detect")
        
        self.midi_map = {} 
        self.learn_target = None 
        self.learn_buttons = []

        self.spark_client = None
        self.seq = 0x20
        self.is_go_or_mini = False
        self.detected_model_name = "SPARK"
        
        self.midi_usb_in = None    
        self.midi_bt_port = None   
        self.last_radar_time = 0
        
        pygame.midi.init()
        self.load_config()
        self.setup_ui()
        
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.start_async_loop, daemon=True).start()
        
        asyncio.run_coroutine_threadsafe(self.spark_search_loop(), self.loop)
        asyncio.run_coroutine_threadsafe(self.midi_search_loop(), self.loop)

    def build_spark_packet(self, cmd_bytes):
        """
        UNIVERSAL PACKET BUILDER FOR ALL SPARK AMPS.
        Uses 9-zero padding for BLE header and circular sequence counter.
        """
        self.seq = (self.seq + 1) & 0xFF
        
        payload = [0xf0, 0x01, self.seq] + cmd_bytes + [0xf7]
        zeros = [0x00] * 9
        
        total_len = 16 + len(payload)
        header = [0x01, 0xfe, 0x00, 0x00, 0x53, 0xfe, total_len]
            
        return bytes(header + zeros + payload)

    def setup_ui(self):
        main = tk.Frame(self.root, bg="#f5f5f5")
        main.pack(expand=True, fill="both") 
        tk.Label(main, text="SPARK MIDI BRIDGE", font=("Arial", 14, "bold"), bg="#f5f5f5", fg="#333").pack(pady=10)
        
        top_controls_frame = tk.Frame(main, bg="#f5f5f5")
        top_controls_frame.pack(pady=5, fill="x")
        center_frame = tk.Frame(top_controls_frame, bg="#f5f5f5")
        center_frame.pack(anchor="center")

        midi_frame = tk.Frame(center_frame, bg="#f5f5f5")
        midi_frame.pack(side="left", padx=15)
        tk.Label(midi_frame, text="MIDI Input:", bg="#f5f5f5", font=("Arial", 9, "bold")).pack(side="left", padx=2)
        self.midi_combo = ttk.Combobox(midi_frame, textvariable=self.midi_device_var, values=["Auto-detect"], state="readonly", width=22)
        self.midi_combo.pack(side="left")
        self.midi_combo.bind("<<ComboboxSelected>>", self.on_midi_change)
        tk.Button(midi_frame, text="Refresh", command=self.refresh_midi_list, bg="#bdc3c7", font=("Arial", 8)).pack(side="left", padx=5)
    
        st_frame = tk.Frame(main, bg="#f5f5f5")
        st_frame.pack(pady=5)
        self.midi_lbl = tk.Label(st_frame, text="PEDAL: DISCONNECTED", fg="#c0392b", bg="#f5f5f5", font=("Arial", 9, "bold"))
        self.midi_lbl.pack(side="left", padx=20)
        self.spark_lbl = tk.Label(st_frame, text="SPARK: DISCONNECTED", fg="#c0392b", bg="#f5f5f5", font=("Arial", 9, "bold"))
        self.spark_lbl.pack(side="left", padx=20)

        grid = tk.Frame(main, bg="white", relief="groove", borderwidth=1)
        grid.pack(pady=10, padx=20, fill="x")
        
        for i in range(4):
            f = tk.Frame(grid, bg="white")
            f.pack(side="left", expand=True, pady=10)
            tk.Label(f, text=f"PRESET {i+1}", bg="white", font=("Arial", 9, "bold")).pack()
            lbl_key = f"preset_lbl_{i}"
            setattr(self, lbl_key, tk.Label(f, text="Not set", fg="#7f8c8d", bg="white", font=("Arial", 8)))
            getattr(self, lbl_key).pack()
            btn = tk.Button(f, text="LEARN", command=lambda idx=i: self.start_learn(idx), bg="#3498db", fg="white", font=("Arial", 8, "bold"), width=8)
            btn.pack(pady=5)
            self.learn_buttons.append(btn)
        
        self.refresh_ui_labels()
        tk.Button(main, text="SAVE CONFIGURATION", command=self.save_config, bg="#2ecc71", fg="white", font=("Arial", 10, "bold"), relief="flat", padx=20).pack(pady=10)

        bottom_frame = tk.Frame(main, bg="#f5f5f5")
        bottom_frame.pack(side="bottom", fill="x", padx=15, pady=(0, 5))
        right_align_frame = tk.Frame(bottom_frame, bg="#f5f5f5")
        right_align_frame.pack(side="right")
        version_lbl = tk.Label(right_align_frame, text=f"Spark MIDI Bridge {self.app_version} |", fg="#7f8c8d", bg="#f5f5f5", font=("Arial", 9))
        version_lbl.pack(side="left")
        link_lbl = tk.Label(right_align_frame, text="RP Forge", fg="#2980b9", bg="#f5f5f5", font=("Arial", 9, "underline", "bold"), cursor="hand2")
        link_lbl.pack(side="left")
        link_lbl.bind("<Button-1>", lambda e: webbrowser.open_new("http://www.youtube.com/@RPForge"))

        self.log_box = tk.Text(main, height=8, font=("Consolas", 9), state="disabled", bg="white")
        self.log_box.pack(padx=10, pady=10, fill="both", expand=True)

    def log(self, msg): self.root.after(0, lambda: self._log(msg))
    def _log(self, msg):
        self.log_box.config(state="normal")
        self.log_box.insert("end", f"> {msg}\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def on_midi_change(self, event=None):
        if self.midi_usb_in:
            try: self.midi_usb_in.close()
            except: pass
            self.midi_usb_in = None
        self.midi_bt_port = None
        selection = self.midi_device_var.get()
        self.log(f"Manual select: {selection}")
        self.root.after(0, lambda: self.midi_lbl.config(text="CONNECTING...", fg="#f39c12"))
        self.last_radar_time = 0

    def start_learn(self, preset_idx):
        if self.learn_target == preset_idx:
            self.learn_target = None
            self.reset_learn_buttons()
            self.log("Learning mode cancelled.")
            return
        self.learn_target = preset_idx
        for i, btn in enumerate(self.learn_buttons):
            if i == preset_idx: 
                btn.config(text="CANCEL", bg="#e74c3c", state="normal") 
            else: 
                btn.config(state="disabled")

    def refresh_ui_labels(self):
        for i in range(4):
            assigned = [k for k, v in self.midi_map.items() if v == f"Preset {i+1}"]
            getattr(self, f"preset_lbl_{i}").config(text=", ".join(assigned) if assigned else "Default (PC)", fg="#2980b9" if assigned else "#7f8c8d")

    async def process_midi_input(self, status, data1):
        if 144 <= status <= 159: msg_type = "NOTE"
        elif 176 <= status <= 191: msg_type = "CC"
        elif 192 <= status <= 207: msg_type = "PC"
        else: return
        midi_key = f"{msg_type}_{data1}"
        if self.learn_target is not None:
            target_name = f"Preset {self.learn_target + 1}"
            self.midi_map = {k: v for k, v in self.midi_map.items() if v != target_name}
            self.midi_map[midi_key] = target_name
            self.log(f"SUCCESS: {midi_key} -> {target_name}")
            self.learn_target = None
            self.root.after(0, self.reset_learn_buttons)
            self.root.after(0, self.refresh_ui_labels)
            return
        target_preset = self.midi_map.get(midi_key)
        if not target_preset and msg_type == "PC": target_preset = f"Preset {(data1 % 4) + 1}"
        if target_preset: await self.send_to_spark(target_preset, midi_key)

    def reset_learn_buttons(self):
        for btn in self.learn_buttons: btn.config(text="LEARN", bg="#3498db", state="normal")

    async def send_to_spark(self, preset_name, midi_key):
        if self.spark_client and self.spark_client.is_connected:
            try:
                preset_idx = int(preset_name.split()[-1]) - 1
                if self.is_go_or_mini:
                    data = [preset_idx, 0x01, 0x38, 0x00, 0x00, preset_idx]
                else:
                    data = [0x15, 0x01, 0x38, 0x00, 0x00, preset_idx]
                
                pkt = self.build_spark_packet(data)
                await self.spark_client.write_gatt_char(WRITE_UUID, pkt, response=False)
                self.log(f"Action: {midi_key} -> {preset_name}")
            except Exception as e:
                self.log(f"Send Error: {e}")

    def on_bt_midi_message(self, sender, args):
        try:
            reader = DataReader.from_buffer(args.message.raw_data)
            data = [reader.read_byte() for _ in range(reader.unconsumed_buffer_length)]
            if len(data) >= 2: asyncio.run_coroutine_threadsafe(self.process_midi_input(data[0], data[1]), self.loop)
        except Exception as e: 
            self.log(f"BT Error: {e}")

    def refresh_midi_list(self):
        self.log("Refreshing MIDI system...")
        self.root.after(0, lambda: self.midi_lbl.config(text="PEDAL: DISCONNECTED", fg="#c0392b"))
        
        if self.midi_usb_in:
            try: self.midi_usb_in.close()
            except: pass
            self.midi_usb_in = None
        self.midi_bt_port = None
        
        pygame.midi.quit()
        pygame.midi.init()
        
        current_sel = self.midi_device_var.get()
        asyncio.run_coroutine_threadsafe(self._scan_midi_devices(current_sel), self.loop)

    async def _scan_midi_devices(self, saved_sel): 
        if self.midi_usb_in:
            try: self.midi_usb_in.close()
            except: pass
            self.midi_usb_in = None
        pygame.midi.quit()
        pygame.midi.init()
        raw = []
        for i in range(pygame.midi.get_count()):
            info = pygame.midi.get_device_info(i)
            if info and info[2]: raw.append(f"[USB] {info[1].decode().strip()}")
        if IS_WINDOWS:
            try:
                selector = midi.MidiInPort.get_device_selector()
                devs = await enumeration.DeviceInformation.find_all_async(selector)
                for d in devs:
                    if d.name: raw.append(f"[BLE] {d.name.strip()}")
            except: pass
        final_list = ["Auto-detect"] + list(dict.fromkeys(raw))
        self.root.after(0, lambda: self.midi_combo.config(values=final_list))
        self.root.after(0, lambda: self.midi_combo.set(saved_sel if saved_sel in final_list else "Auto-detect"))
        self.log(f"Scan complete. Found {len(final_list)-1} devices.")

    def start_async_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def spark_search_loop(self):
        while True:
            if not self.spark_client or not self.spark_client.is_connected:
                try:
                    self.spark_lbl.config(text="SPARK: SCANNING", fg="#f39c12")
                    devs = await BleakScanner.discover(timeout=4.0)
                    target = None
                    for d in devs:
                        name = str(d.name if d.name else "").lower()
                        address = str(d.address).upper()
                        if "spark" in name or any(address.startswith(p) for p in SPARK_MAC_PREFIXES):
                            target = d
                            break
                    if target:
                        name_lower = str(target.name if target.name else "").lower()
                        self.is_go_or_mini = ("go" in name_lower) or ("mini" in name_lower)
                        
                        if "go" in name_lower: self.detected_model_name = "SPARK GO"
                        elif "mini" in name_lower: self.detected_model_name = "SPARK MINI"
                        elif "2" in name_lower: self.detected_model_name = "SPARK 2"
                        else: self.detected_model_name = "SPARK 40"
                        
                        self.log(f"Connecting to: {target.name} ({self.detected_model_name})...")
                        self.spark_client = BleakClient(target.address)
                        await self.spark_client.connect()
                        
                        self.seq = 0x20
                        
                        # Subscribe to notifications to activate session for all models
                        def notification_handler(sender, data: bytearray):
                            pass
                        try:
                            await self.spark_client.start_notify(NOTIFY_UUID, notification_handler)
                        except Exception as ne:
                            self.log(f"Notify init: {ne}")
                            
                        self.spark_lbl.config(text=f"{self.detected_model_name}: ONLINE", fg="#27ae60")
                        self.log(f"Connection established with {self.detected_model_name}.")
                except Exception as e:
                    self.spark_lbl.config(text="SPARK: DISCONNECTED", fg="#c0392b")
                    self.spark_client = None
            await asyncio.sleep(5)

    async def midi_search_loop(self):
        self.last_radar_time = 0 

        while True:
            selection = self.midi_device_var.get()
            clean_sel = selection.replace("[USB] ", "").replace("[BLE] ", "").lower().strip()
            
            current_time = time.time()
            run_radar = (current_time - self.last_radar_time > 2.0)
            if run_radar:
                self.last_radar_time = current_time

            # --- 1. USB Search ---
            if not self.midi_usb_in and not selection.startswith("[BLE]"):
                if run_radar:
                    pygame.midi.quit()
                    pygame.midi.init()
                    
                    for i in range(pygame.midi.get_count()):
                        info = pygame.midi.get_device_info(i)
                        if info and info[2]:
                            name = info[1].decode().strip().replace('\x00', '')
                            is_match = False
                            if selection == "Auto-detect":
                                if any(k in name.lower() for k in ["foot", "midi", "m-vave", "sinco"]): is_match = True
                            elif clean_sel in name.lower(): is_match = True
                            
                            if is_match:
                                try:
                                    self.midi_usb_in = pygame.midi.Input(i)
                                    self.current_usb_name = name
                                    self.root.after(0, lambda n=name: self.midi_lbl.config(text="PEDAL: USB READY", fg="#27ae60"))
                                    self.log(f"Linked USB: {name}")
                                    break
                                except: pass

            # --- 2. USB Reading and Radar ---
            if self.midi_usb_in:
                try:
                    if self.midi_usb_in.poll():
                        for event in self.midi_usb_in.read(10):
                            asyncio.run_coroutine_threadsafe(self.process_midi_input(event[0][0], event[0][1]), self.loop)
                except:
                    try: self.midi_usb_in.close()
                    except: pass
                    self.midi_usb_in = None
                    self.root.after(0, lambda: self.midi_lbl.config(text="PEDAL: DISCONNECTED", fg="#c0392b"))

                if IS_WINDOWS and hasattr(self, 'current_usb_name') and run_radar:
                    try:
                        selector = midi.MidiInPort.get_device_selector()
                        all_devs = await enumeration.DeviceInformation.find_all_async(selector)
                        still_alive = any(self.current_usb_name in d.name for d in all_devs if d.name)
                        if not still_alive:
                            self.midi_usb_in.close()
                            self.midi_usb_in = None
                            self.root.after(0, lambda: self.midi_lbl.config(text="PEDAL: DISCONNECTED", fg="#c0392b"))
                            self.log("USB cable disconnected.")
                    except: pass

            # --- 3. BT Search and Radar ---
            if IS_WINDOWS and not self.midi_usb_in and not selection.startswith("[USB]"):
                if not self.midi_bt_port:
                    if run_radar:
                        try:
                            bt_devs = await enumeration.DeviceInformation.find_all_async()
                            target = None
                            if selection == "Auto-detect":
                                target = next((d for d in bt_devs if d.name and any(x in d.name.lower() for x in ["foot", "m-vave", "sinco"]) and "midi" in d.name.lower()), None)
                            else:
                                target = next((d for d in bt_devs if d.name and clean_sel in d.name.lower()), None)
                            
                            if target:
                                port = await midi.MidiInPort.from_id_async(target.id)
                                if port:
                                    self.midi_bt_port = port
                                    self.midi_bt_port.add_message_received(self.on_bt_midi_message)
                                    self.root.after(0, lambda: self.midi_lbl.config(text="PEDAL: BLE READY", fg="#27ae60"))
                                    self.log(f"Linked BLE: {target.name}")
                        except: pass
                else:
                    if run_radar:
                        try:
                            bt_devs = await enumeration.DeviceInformation.find_all_async()
                            current_id = self.midi_bt_port.device_id
                            if not any(d.id == current_id for d in bt_devs):
                                self.midi_bt_port = None
                                self.root.after(0, lambda: self.midi_lbl.config(text="PEDAL: DISCONNECTED", fg="#c0392b"))
                                self.log("BLE connection lost.")
                        except:
                            self.midi_bt_port = None
                            self.root.after(0, lambda: self.midi_lbl.config(text="PEDAL: DISCONNECTED", fg="#c0392b"))

            await asyncio.sleep(0.1)

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    d = json.load(f)
                    self.midi_map = d.get("midi_map", {})
            except: pass

    def save_config(self):
        with open(CONFIG_FILE, "w") as f: json.dump({"midi_map": self.midi_map}, f, indent=4)
        messagebox.showinfo("Success", "Saved!")

if __name__ == "__main__":
    multiprocessing.freeze_support()
    root = tk.Tk()
    app = SparkMidiApp(root)
    root.mainloop()
