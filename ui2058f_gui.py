import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import math, time, sys, struct, select, threading, queue
import numpy as np
import os
import datetime

try:
    import pandas as pd
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    print("ОШИБКА: Для работы с Excel установите библиотеки: pip install pandas openpyxl")
    sys.exit(1)

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("Нужен pyserial: pip install pyserial")
    sys.exit(1)

# ============================================================
# НАСТРОЙКИ ДОПУСКОВ (Метрологическая модель: % от показания + абс. смещение)
# ============================================================
TOL_U_PCT = 1.0       # Допуск по напряжению (% от Ref)
TOL_U_ABS = 0.25      # Аддитивный допуск по напряжению (В)

TOL_I_MA_PCT = 1.2    # Допуск по току (% от Ref)
TOL_I_MA_ABS = 1.5    # Аддитивный допуск по току (мА)

TOL_P_PCT = 1.5       # Допуск по мощности (% от Ref)
TOL_P_ABS = 0.5       # Аддитивный допуск по мощности (Вт)

# Границы адекватности самих калибровочных коэффициентов (отлов "кривого" железа)
MAX_GAIN_DEV = 0.05   # Коэффициент 'a' должен быть в пределах 0.95 ... 1.05

# ============================================================
# МОДУЛЬ СВЯЗИ С UI2058 
# ============================================================
class TcpSerial:
    def __init__(self, host, port, timeout=3.0):
        import socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect((host, port))
        try: self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception: pass
        self._buf = bytearray()
        self._timeout = timeout

    @property
    def in_waiting(self):
        try: r, _, _ = select.select([self.sock], [], [], 0)
        except Exception: return len(self._buf)
        if r:
            try:
                data = self.sock.recv(65536)
                if data: self._buf += data
            except Exception: pass
        return len(self._buf)

    def read(self, n=1):
        if not self._buf: return b''
        data = bytes(self._buf[:n]); del self._buf[:n]
        return data

    def write(self, b):
        self.sock.sendall(b); return len(b)
    def flush(self): pass

    def reset_input_buffer(self):
        end = time.time() + 0.3
        while time.time() < end:
            try: r, _, _ = select.select([self.sock], [], [], 0.02)
            except Exception: break
            if r:
                try: self.sock.recv(65536)
                except Exception: break
        self._buf.clear()

    def close(self):
        try: self.sock.close()
        except Exception: pass

def drain(ser, ms=800):
    t0 = time.time(); buf = bytearray()
    while (time.time() - t0) * 1000 < ms:
        n = ser.in_waiting
        if n: buf += ser.read(n)
        else: time.sleep(0.005)
    return bytes(buf)

def cmd(ser, b, ms=800):
    ser.write(b); ser.flush()
    return drain(ser, ms)

def as_i16(data, off=0):
    raw = data[off:]
    n = len(raw) - (len(raw) % 2)
    if n < 2: return np.array([], dtype=float)
    return np.frombuffer(raw[:n], dtype='<i2').astype(float)

def safe_resample(sig, n=256):
    s = np.asarray(sig, dtype=float).flatten()
    if len(s) >= n: return s[:n].copy()
    if len(s) == 0: return np.zeros(n)
    return np.tile(s, (n + len(s) - 1) // len(s))[:n]

def _is_M(v5):
    if len(v5) < 5: return False
    u, i, p, pf, f = v5[:5]
    return (0.1 < u < 600 and 0.0001 < i < 50 and -10000 < p < 10000 and -2.0 <= pf <= 2.0 and 10 < f < 100)

def find_M(data):
    for off in range(0, len(data) - 20 + 1):
        try: v5 = struct.unpack_from('<5f', data, off)
        except Exception: continue
        if _is_M(v5):
            try: 
                full_pack = struct.unpack_from('<15f', data, off)
                return off, full_pack
            except Exception: 
                return off, v5 + (0.0,) * 10
    return None, None

def find_M_in_chunks(chunks):
    for i, c in enumerate(chunks):
        if len(c) > 520:
            off, v = find_M(c)
            if v is not None: return i, off, v
    for i, c in enumerate(chunks):
        off, v = find_M(c)
        if v is not None: return i, off, v
    return None, None, None

def harmonics(sig, nh=40):
    x = np.asarray(sig, dtype=float).flatten()
    if len(x) < 32: return np.zeros(nh)
    dc = abs(float(np.mean(x)))
    xc = x - x.mean()
    n = len(xc)
    a = np.abs(np.fft.rfft(xc, n=n)) * np.sqrt(2.0) / n
    result = np.zeros(nh)
    result[0] = dc
    result[1:] = a[1:nh]
    return result

def collect_signals(chunks, skip_chunk=None):
    out = []
    for i, c in enumerate(chunks):
        if i == skip_chunk or len(c) < 400: continue
        for off in range(0, min(10, len(c) - 400)):
            sig = as_i16(c, off)
            if len(sig) < 200: continue
            ptp = float(np.ptp(sig))
            if ptp < 50 or ptp > 10000: continue
            a = harmonics(sig)
            if a[1] < 1e-9: continue
            thd = float(np.sqrt(np.sum(a[2:]**2)) / a[1] * 100)
            if thd > 80: continue
            rms = float(np.sqrt(np.mean(sig**2)))
            if rms < 20: continue
            out.append({'chunk': i, 'off': off, 'sig': sig, 'amp': a, 'thd': thd, 'rms': rms, 'ptp': ptp})
    out.sort(key=lambda w: (w['chunk'], w['off']))
    uniq = []
    for w in out:
        if not any(u['chunk'] == w['chunk'] and abs(u['off'] - w['off']) <= 3 for u in uniq):
            uniq.append(w)
    return uniq

def zero_cross(u, n=256):
    u = np.asarray(u, dtype=float)[:n]
    for k in range(n - 1):
        if u[k] <= 0 and u[k + 1] > 0:
            d = u[k] - u[k + 1]
            return k + (u[k] / d if abs(d) > 1e-12 else 0.0)
    return 0.0

def phase_ui2058(i_sig, n=256, pct=5.0):
    i = safe_resample(i_sig, n) - safe_resample(i_sig, n).mean()
    pk = float(np.max(i))
    if pk <= 0: return 0.0, 90.0, 180.0
    above = i > pk * pct / 100.0
    if not above.any(): return 0.0, 90.0, 180.0
    d = np.diff(above.astype(int))
    starts = list(np.where(d == 1)[0])
    ends = list(np.where(d == -1)[0])
    if above[0] and (not starts or starts[0] > 0): starts = [0] + starts
    if above[-1] and (not ends or ends[-1] < n - 1): ends = ends + [n - 1]
    if not starts or not ends: return 0.0, 90.0, 180.0
    z0 = int(starts[0])
    beg = (z0 * 360.0 / n) % 360
    end = 180.0
    return beg, (beg + end) / 2.0, end

def read_packet(ser):
    chunks = []
    for attempt in range(20):
        d = cmd(ser, b'\x21', 700)
        if not d: 
            if chunks: break
            else: 
                time.sleep(0.1)
                continue
        chunks.append(d)
        if len(d) < 10 and chunks: break
        if len(chunks) >= 5 and sum(len(c) for c in chunks) > 2000: break
    return chunks

def measure(ser):
    for attempt in range(5):
        cmd(ser, b'\x3f', 200)
        cmd(ser, b'\x32', 200)
        time.sleep(0.2)
        chunks = read_packet(ser)
        if not chunks: continue
        M_ci, Moff, M = find_M_in_chunks(chunks)
        if M is None: continue
        sigs = collect_signals(chunks, skip_chunk=M_ci)
        if not sigs: continue
        
        U = min(sigs, key=lambda w: w['thd'])
        others = [w for w in sigs if w is not U]
        I = max(others, key=lambda w: w['thd']) if others else U
        scale = M[1] / (I['rms'] + 1e-12)
        amps = I['amp'] * scale
        i1 = abs(amps[1]) + 1e-12
        thdi = float(np.sqrt(np.sum(amps[2:]**2)) / i1 * 100)
        u = safe_resample(U['sig'], 256); u = u - u.mean()
        v0 = zero_cross(u, 256); sh = int(v0); fr = v0 - sh
        i_sh = np.roll(safe_resample(I['sig'], 256), -sh)
        if fr > 1e-6:
            xs = np.arange(256)
            i_sh = np.interp(xs + fr, xs, i_sh)

        vlamp_cv = 0.0
        pout_cv = 0.0
        vlamp_cc = 0.0
        ilamp = 0.0
        pout_cc = 0.0
        eta = 0.0
        
        if len(chunks) > 0 and len(chunks[-1]) >= 20:
            try:
                vlamp_cv = struct.unpack_from('<f', chunks[-1], 0)[0]
                vlamp_cc = struct.unpack_from('<f', chunks[-1], 0)[0]
                ilamp = struct.unpack_from('<f', chunks[-1], 4)[0]
                eta = struct.unpack_from('<f', chunks[-1], 16)[0]
            except Exception: pass
            
        if len(chunks) > 2 and len(chunks[2]) > 14:
            try:
                pout_int = struct.unpack_from('<h', chunks[2], 12)[0]
                pout_cv = pout_int / 100.0
                pout_cc = pout_int / 100.0
            except Exception: pass

        return {'urms': M[0], 'irms': M[1], 'p': M[2], 'pf': M[3], 'freq': M[4], 
                'vlamp_cv': vlamp_cv, 'pout_cv': pout_cv,
                'ilamp': ilamp, 'vlamp_cc': vlamp_cc, 'pout_cc': pout_cc, 'eta': eta,
                'amps': amps, 'i1': i1, 'thdi': thdi}
    return None

# ============================================================
# МАТЕМАТИКА
# ============================================================
def linreg(xs, ys):
    valid = [(x, y) for x, y in zip(xs, ys) if x != 0.0 and y != 0.0]
    n = len(valid)
    if n < 2: return 1.0, 0.0
    
    mx = sum(p[0] for p in valid) / n
    my = sum(p[1] for p in valid) / n
    sxx = sum((p[0] - mx) ** 2 for p in valid)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in valid)
    if sxx == 0: return 1.0, 0.0
    
    a = sxy / sxx
    return a, my - a * mx

def calc_input(rows):
    uref, udev, iref, idev, pref, pdev = [], [], [], [], [], []
    dU, dI, dP = [], [], []
    
    for ur, ud, ir, id_, pf_ref, pf_dev in rows:
        pr = ur * ir * pf_ref / 1000.0
        pd = ud * id_ * pf_dev / 1000.0
        uref.append(ur); udev.append(ud); iref.append(ir); idev.append(id_)
        pref.append(pr); pdev.append(pd)
        dU.append(ur - ud); dI.append(ir - id_); dP.append(pr - pd)
    
    aU, bU = linreg(udev, uref)
    aI, bI = linreg(idev, iref)
    aP, bP = linreg(pdev, pref)
    
    return {
        "rows": list(zip(uref, udev, iref, idev, pref, pdev, dU, dI, dP)),
        "kU": aU, "kI": aI, "kP": aP,
        "bU": bU, "bI": bI, "bP": bP,
        "uref": uref, "udev": udev, "iref": iref, "idev": idev,
        "pref": pref, "pdev": pdev
    }

def calc_cv(rows):
    out = []
    vl_refs, vl_devs = [], []
    il_refs, il_devs = [], []
    p_refs, p_devs = [], []
    
    for row in rows:
        label, vl_ref, vl_dev, il_ref, il_dev, v_rms, i_rms, pf, eta = row
        calc_pout_ref = (vl_ref * il_ref)
        calc_pout_dev = (vl_dev * il_dev)
        
        vl_refs.append(vl_ref); vl_devs.append(vl_dev)
        il_refs.append(il_ref); il_devs.append(il_dev)
        p_refs.append(calc_pout_ref); p_devs.append(calc_pout_dev)
        
        out.append({
            "label": label, "vl_ref": vl_ref, "vl_dev": vl_dev, "il_ref": il_ref, "il_dev": il_dev, 
            "pout_ref": calc_pout_ref, "pout_dev": calc_pout_dev,
            "v_rms": v_rms, "i_rms": i_rms, "pf": pf, "eta": eta
        })
    
    aVL, bVL = linreg(vl_devs, vl_refs)
    aIL, bIL = linreg(il_devs, il_refs)
    aP, bP = linreg(p_devs, p_refs)
    
    return {
        "rows": out, 
        "k_VL": aVL, "k_IL": aIL, "k_P": aP,
        "b_VL": bVL, "b_IL": bIL, "b_P": bP
    }

def calc_cc(rows):
    out = []
    il_refs, il_devs = [], []
    vl_refs, vl_devs = [], []
    p_refs, p_devs = [], []
    
    for label, ir, id_, uo, uin, vrms, irms, pf, eta in rows:
        pout_ref = uo * ir
        pout_dev = uin * id_
        pin = vrms * irms * pf
        
        il_refs.append(ir); il_devs.append(id_)
        vl_refs.append(uo); vl_devs.append(uin)
        p_refs.append(pout_ref); p_devs.append(pout_dev)
        
        out.append({
            "label": label, "iref": ir, "idev": id_, "uout": uo, "uout_dev": uin, 
            "pout_ref": pout_ref, "pout_dev": pout_dev, "pin": pin, "eta": eta, "di": ir - id_
        })
        
    aI, bI = linreg(il_devs, il_refs)
    aV, bV = linreg(vl_devs, vl_refs)
    aP, bP = linreg(p_devs, p_refs)
    
    return {"rows": out, "kIout": aI, "bIout": bI, "kVout": aV, "bVout": bV, "kPout": aP, "bPout": bP}

# ============================================================
# ИНТЕРФЕЙС
# ============================================================
def get_float(e):
    try:
        val = float(str(e.get()).replace(",", ".").replace(" ", "").strip())
        if math.isnan(val) or math.isinf(val):
            return 0.0
        return val
    except:
        return 0.0

def make_text_with_scroll(parent, height=20):
    f = ttk.Frame(parent)
    txt = tk.Text(f, height=height, font=("Courier New", 9), wrap="none")
    vsb = ttk.Scrollbar(f, orient="vertical", command=txt.yview)
    hsb = ttk.Scrollbar(f, orient="horizontal", command=txt.xview)
    txt.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    txt.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns"); hsb.grid(row=1, column=0, sticky="ew")
    f.rowconfigure(0, weight=1); f.columnconfigure(0, weight=1)
    return f, txt

def get_pd_str(row, col):
    if col in row and pd.notna(row[col]):
        val_str = str(row[col]).strip()
        if val_str == "" or val_str.lower() == "nan":
            return ""
        try:
            num = float(val_str.replace(",", "."))
            return "" if num == 0.0 else str(num)
        except ValueError:
            return val_str
    return ""

class App:
    def __init__(self, root):
        self.root = root
        root.title("UI2088F — Расчет калибровки")
        root.geometry("1600x950") 
        
        self.ser = None
        self.read_queue = queue.Queue()
        self.status = tk.StringVar(value="Готов")
        self.all_read_buttons = []
        
        style = ttk.Style()
        style.configure("Ref.TEntry", fieldbackground="#e8e8e8", foreground="#000080")
        style.configure("Dev.TEntry", fieldbackground="#ffffff", foreground="#000")
        style.configure("DevFail.TEntry", fieldbackground="#ffcccc", foreground="#000") 
        style.configure("Green.TEntry", fieldbackground="#d4edda", foreground="#155724")
        style.map("DevFail.TEntry", fieldbackground=[("readonly", "#ffcccc"), ("!disabled", "#ffcccc")])
        
        menubar = tk.Menu(root)
        fm = tk.Menu(menubar, tearoff=0)
        fm.add_command(label="Загрузить из Excel...", command=self.load_excel)
        fm.add_command(label="Сохранить в Excel...", command=self.save_excel)
        fm.add_separator()
        fm.add_command(label="Выход", command=root.quit)
        menubar.add_cascade(label="Файл", menu=fm)
        root.config(menu=menubar)

        self.build_connection_panel()

        self.main_container = ttk.Frame(root)
        self.main_container.pack(fill="both", expand=True, padx=6, pady=6)

        self.left_frame = ttk.Frame(self.main_container)
        self.left_frame.pack(side="left", fill="both", expand=True)

        self.nb = ttk.Notebook(self.left_frame)
        self.nb.pack(fill="both", expand=True)

        self.tab_in = ttk.Frame(self.nb, padding=8)
        self.tab_cv = ttk.Frame(self.nb, padding=8)
        self.tab_cc = ttk.Frame(self.nb, padding=8)
        self.tab_valid = ttk.Frame(self.nb, padding=8)
        self.tab_instr = ttk.Frame(self.nb, padding=8)

        self.nb.add(self.tab_in, text="1. Вход (12 точек)")
        self.nb.add(self.tab_cv, text="2. Выход CV")
        self.nb.add(self.tab_cc, text="3. Выход CC")
        self.nb.add(self.tab_valid, text="4. Валидация")
        self.nb.add(self.tab_instr, text="5. Инструкция")

        self.right_frame = ttk.LabelFrame(self.main_container, text="Коэффициенты (Реальное время)", padding=10)
        self.right_frame.pack(side="right", fill="y", padx=(10, 0))
        self.build_right_panel()

        ttk.Label(root, textvariable=self.status, relief="sunken", anchor="w", font=("Arial", 9)).pack(fill="x", side="bottom")

        self.build_in_tab()
        self.build_cv_tab()
        self.build_cc_tab()
        self.build_valid_tab()
        self.build_instr_tab()

        self.recalc_all()
        self.root.after(100, self.check_read_queue)

    def build_connection_panel(self):
        f = ttk.LabelFrame(self.root, text="Подключение к прибору и управление файлами")
        f.pack(fill="x", padx=6, pady=4)
        
        ttk.Label(f, text="IP-адрес:").grid(row=0, column=0, padx=4, pady=4)
        self.ent_ip = ttk.Entry(f, width=15); self.ent_ip.insert(0, "172.16.8.145")
        self.ent_ip.grid(row=0, column=1, padx=4)
        
        ttk.Label(f, text="Порт:").grid(row=0, column=2, padx=4)
        self.ent_port = ttk.Entry(f, width=6); self.ent_port.insert(0, "4001")
        self.ent_port.grid(row=0, column=3, padx=4)
        
        self.btn_connect = ttk.Button(f, text="Подключить", command=self.toggle_connection)
        self.btn_connect.grid(row=0, column=4, padx=10)
        
        self.lbl_conn = ttk.Label(f, text="Отключено", foreground="red")
        self.lbl_conn.grid(row=0, column=5, padx=10)

        ttk.Label(f, text="Тип прибора:").grid(row=0, column=6, padx=(20, 4), pady=4)
        self.ent_dev_type = ttk.Entry(f, width=15)
        self.ent_dev_type.grid(row=0, column=7, padx=4)
        
        ttk.Label(f, text="S/N:").grid(row=0, column=8, padx=4, pady=4)
        self.ent_dev_sn = ttk.Entry(f, width=15)
        self.ent_dev_sn.grid(row=0, column=9, padx=4)

        ttk.Button(f, text="Загрузить из Excel", command=self.load_excel).grid(row=0, column=10, padx=10)
        ttk.Button(f, text="Сохранить в Excel", command=self.save_excel).grid(row=0, column=11, padx=10)

    def build_right_panel(self):
        self.coef_labels = {}
        
        def add_row(key, text):
            frame = ttk.Frame(self.right_frame)
            frame.pack(fill="x", pady=5)
            ttk.Label(frame, text=text, width=15).pack(side="left")
            lbl = ttk.Label(frame, text="---", font=("Courier New", 11, "bold"), foreground="#000080")
            lbl.pack(side="right")
            self.coef_labels[key] = lbl

        ttk.Label(self.right_frame, text="ВХОД (Input):", font=("Arial", 9, "bold"), foreground="#555").pack(anchor="w", pady=(0, 5))
        add_row("kU", "k_U_in")
        add_row("kI", "k_I_in")
        add_row("kP", "k_P_in")
        
        ttk.Separator(self.right_frame, orient="horizontal").pack(fill="x", pady=10)
        
        ttk.Label(self.right_frame, text="ВЫХОД CV:", font=("Arial", 9, "bold"), foreground="#555").pack(anchor="w", pady=(0, 5))
        add_row("kVL", "k_V_Lamp")
        add_row("kIL", "k_I_Lamp")
        add_row("kPoutCV", "k_Pout")
        
        ttk.Separator(self.right_frame, orient="horizontal").pack(fill="x", pady=10)
        
        ttk.Label(self.right_frame, text="ВЫХОД CC:", font=("Arial", 9, "bold"), foreground="#555").pack(anchor="w", pady=(0, 5))
        add_row("kICC", "k_I_Lamp")
        add_row("kVC", "k_V_Lamp")
        add_row("kPoutCC", "k_Pout")

    def toggle_connection(self):
        for btn in self.all_read_buttons:
            btn.config(bg="#f0f0f0", fg="blue", text="R", state="normal")
            
        if self.ser:
            try: self.ser.close()
            except: pass
            self.ser = None
            self.btn_connect.config(text="Подключить")
            self.lbl_conn.config(text="Отключено", foreground="red")
            self.status.set("Отключено от прибора")
        else:
            ip = self.ent_ip.get().strip()
            port = int(self.ent_port.get().strip())
            self.status.set(f"Подключение к {ip}:{port}...")
            try:
                self.ser = TcpSerial(ip, port, timeout=3.0)
                self.ser.reset_input_buffer()
                cmd(self.ser, b'\x76', 500)
                self.btn_connect.config(text="Отключить")
                self.lbl_conn.config(text="Подключено", foreground="green")
                self.status.set(f"Успешно подключено к {ip}:{port}")
            except Exception as e:
                self.ser = None
                messagebox.showerror("Ошибка", f"Не удалось подключиться:\n{e}")
                self.status.set("Ошибка подключения")

    def read_device_thread(self, target_type, idx, btn):
        r = measure(self.ser)
        self.read_queue.put((target_type, idx, r, btn))

    def check_read_queue(self):
        try:
            target_type, idx, r, btn = self.read_queue.get_nowait()
            if r:
                ud_val = r['urms']
                id_val = r['irms']
                pf_val = r['pf']
                p_val = r['p']
                f_val = r['freq']
                
                ilamp = r['ilamp']
                eta = r['eta']
                
                widgets_to_highlight = []
                
                if target_type == "INPUT":
                    g_idx = idx // 3
                    p_idx = idx % 3
                    g = self.in_entries[g_idx]
                    g["u"][p_idx][1].delete(0, tk.END); g["u"][p_idx][1].insert(0, f"{ud_val:.3f}")
                    g["i"][p_idx][1].delete(0, tk.END); g["i"][p_idx][1].insert(0, f"{id_val * 1000.0:.4f}")
                    widgets_to_highlight = [g["u"][p_idx][1], g["i"][p_idx][1]]
                    
                elif target_type == "CV":
                    vlamp = r['vlamp_cv']
                    pout_dev = r['pout_cv']
                    row = self.cv_entries[idx]
                    row[2].delete(0, tk.END); row[2].insert(0, f"{vlamp:.3f}") 
                    row[4].delete(0, tk.END); row[4].insert(0, f"{ilamp:.4f}") 
                    row[6].config(state="normal")
                    row[6].delete(0, tk.END); row[6].insert(0, f"{pout_dev:.3f}")
                    row[6].config(state="readonly")
                    row[7].delete(0, tk.END); row[7].insert(0, f"{eta:.4f}") 
                    row[8].delete(0, tk.END); row[8].insert(0, f"{ud_val:.3f}") 
                    row[9].delete(0, tk.END); row[9].insert(0, f"{id_val:.4f}") 
                    row[10].delete(0, tk.END); row[10].insert(0, f"{pf_val:.3f}") 
                    widgets_to_highlight = [row[2], row[4], row[6], row[7], row[8], row[9], row[10]]
                    
                elif target_type == "CC":
                    vlamp = r['vlamp_cc']
                    pout_dev = r['pout_cc']
                    row = self.cc_entries[idx]
                    row[2].delete(0, tk.END); row[2].insert(0, f"{ilamp:.4f}") 
                    row[4].delete(0, tk.END); row[4].insert(0, f"{vlamp:.3f}") 
                    row[8].delete(0, tk.END); row[8].insert(0, f"{ud_val:.3f}") 
                    row[9].delete(0, tk.END); row[9].insert(0, f"{id_val:.4f}") 
                    row[10].delete(0, tk.END); row[10].insert(0, f"{pf_val:.3f}") 
                    row[7].delete(0, tk.END); row[7].insert(0, f"{eta:.4f}")
                    widgets_to_highlight = [row[2], row[4], row[8], row[9], row[10], row[7]]
                
                for w in widgets_to_highlight:
                    w.config(style="Green.TEntry")
                    self.root.after(2000, lambda ww=w: ww.config(style="Dev.TEntry"))
                
                btn.config(bg="#228B22", fg="white", text="OK", state="normal")
                
                if target_type == "CC" or target_type == "CV":
                    self.status.set(f"Считано: V_Lamp={vlamp:.3f}В, I_Lamp={ilamp:.4f}А, Pout={pout_dev:.3f}Вт, Eff={eta:.4f}")
                else:
                    self.status.set(f"Считано: V_RMS={ud_val:.3f}В, I_RMS={id_val*1000:.4f}мА, P={p_val:.3f}Вт, PF={pf_val:.3f}")
                self.recalc_all()
            else:
                messagebox.showwarning("Ошибка", "Не удалось считать данные. Проверьте подключение и консоль.")
                self.status.set("Ошибка чтения")
                btn.config(bg="#D32F2F", fg="white", text="X", state="normal")
                self.root.after(3000, lambda b=btn: b.config(bg="#f0f0f0", fg="blue", state="normal", text="R"))
        except queue.Empty:
            pass
        self.root.after(100, self.check_read_queue)

    def read_btn_click(self, target_type, idx, btn):
        if not self.ser:
            messagebox.showwarning("Внимание", "Сначала подключитесь к прибору по IP!")
            return
        btn.config(state="disabled", bg="#FFD700", fg="black", text="...")
        self.status.set("Чтение данных с прибора...")
        threading.Thread(target=self.read_device_thread, args=(target_type, idx, btn), daemon=True).start()

    # ============================================================
    # ВКЛАДКИ
    # ============================================================
    def build_in_tab(self):
        f = self.tab_in
        ttk.Label(f, text="Входные параметры (тестируются на резисторах)", font=("Arial", 11, "bold")).pack(anchor="w", pady=(0, 5))
        ttk.Label(f, text="Серые поля = Ref (вручную). Белые = Dev (считывается по [R]). Зеленые цифры = Ватты.", foreground="#555").pack(anchor="w")
        
        grid_f = ttk.Frame(f); grid_f.pack(anchor="w", pady=15, padx=10)
        self.in_entries = []
        
        headers = ["", "", "Uвх = 176 В", "", "", "Uвх = 220 В", "", "", "Uвх = 264 В", "", ""]
        for c, h in enumerate(headers):
            if h: ttk.Label(grid_f, text=h, font=("Arial", 10, "bold"), foreground="#000080").grid(row=0, column=c, columnspan=3, pady=(0, 5))
        
        for i, col_start in enumerate([2, 5, 8]):
            ttk.Label(grid_f, text="Ref", font=("Arial", 9, "bold"), foreground="#000080").grid(row=1, column=col_start, padx=5)
            ttk.Label(grid_f, text="Dev", font=("Arial", 9, "bold"), foreground="black").grid(row=1, column=col_start+1, padx=5)

        groups = ["P~7 Вт", "P~50 Вт", "P~100 Вт", "P~200 Вт"]
        row_idx = 2
        for group_idx in range(4):
            ttk.Label(grid_f, text=groups[group_idx], font=("Arial", 10, "bold"), foreground="black").grid(row=row_idx, column=8, columnspan=3, pady=(0, 5))

            ttk.Label(grid_f, text="Uвх, В").grid(row=row_idx+1, column=1, sticky="e", padx=5)
            u_entries = []
            for i in range(3):
                data_idx = group_idx * 3 + i
                col_base = i * 3 + 2
                ur = ttk.Entry(grid_f, width=8, style="Ref.TEntry", justify="center")
                ud = ttk.Entry(grid_f, width=8, style="Dev.TEntry", justify="center")
                btn = tk.Button(grid_f, text="R", width=2, fg="blue", bg="#f0f0f0", relief="raised", font=("Arial", 8, "bold"))
                btn.configure(command=lambda idx=data_idx, b=btn: self.read_btn_click("INPUT", idx, b))
                self.all_read_buttons.append(btn)
                
                ur.grid(row=row_idx+1, column=col_base, padx=(5, 2), pady=2)
                ud.grid(row=row_idx+1, column=col_base+1, padx=(2, 2), pady=2)
                btn.grid(row=row_idx+1, column=col_base+2, padx=(2, 10), pady=2)
                u_entries.append((ur, ud))
            
            ttk.Label(grid_f, text="Iвх, мА").grid(row=row_idx+2, column=1, sticky="e", padx=5) 
            i_entries = []
            for i in range(3):
                col_base = i * 3 + 2
                ir = ttk.Entry(grid_f, width=8, style="Ref.TEntry", justify="center")
                id_ = ttk.Entry(grid_f, width=8, style="Dev.TEntry", justify="center")
                ir.grid(row=row_idx+2, column=col_base, padx=(5, 2), pady=2)
                id_.grid(row=row_idx+2, column=col_base+1, padx=(2, 2), pady=2)
                i_entries.append((ir, id_))
                
            ttk.Label(grid_f, text="Pвх, Вт").grid(row=row_idx+3, column=1, sticky="e", padx=5)
            p_labels = []
            for i in range(3):
                col_base = i * 3 + 2
                pr_lbl = ttk.Label(grid_f, text="0.00", width=8, foreground="#000080", font=("Courier New", 9, "bold")); pr_lbl.grid(row=row_idx+3, column=col_base, padx=(5, 2), pady=2)
                pd_lbl = ttk.Label(grid_f, text="0.00", width=8, foreground="#008000", font=("Courier New", 9, "bold")); pd_lbl.grid(row=row_idx+3, column=col_base+1, padx=(2, 2), pady=2)
                p_labels.append((pr_lbl, pd_lbl))
            
            self.in_entries.append({"u": u_entries, "i": i_entries, "p": p_labels})
            row_idx += 4
            ttk.Separator(grid_f, orient="horizontal").grid(row=row_idx, column=0, columnspan=11, sticky="ew", pady=8)
            row_idx += 1

        bf = ttk.Frame(f); bf.pack(anchor="w", pady=6)
        ttk.Button(bf, text="Пересчитать вручную", command=self.recalc_all).pack(side="left", padx=5)
        ttk.Button(bf, text="Очистить всё", command=self.clear_input).pack(side="left", padx=5)
        
        tf, self.in_text = make_text_with_scroll(f, height=10)
        tf.pack(fill="both", expand=True, pady=4)

    def clear_input(self):
        for g in self.in_entries:
            for i in range(3):
                g["u"][i][0].delete(0, tk.END); g["u"][i][1].delete(0, tk.END)
                g["i"][i][0].delete(0, tk.END); g["i"][i][1].delete(0, tk.END)
        self.recalc_all()

    def build_cv_tab(self):
        f = self.tab_cv
        ttk.Label(f, text="Выход CV (стабилизация напряжения)", font=("Arial", 11, "bold")).pack(anchor="w")
        ttk.Label(f, text="Введите Ref значения и нажмите [R] для считывания Dev. Pout Ref считается автоматически.", foreground="#555").pack(anchor="w", pady=(0,10))
        
        grid_f = ttk.Frame(f); grid_f.pack(anchor="w")
        headers = ["Метка", "V_Lamp Ref, В", "V_Lamp, В", "I_Lamp Ref, А", "I_Lamp, А", "Pout Ref, Вт", "Pout, Вт", "Efficiency", "V_RMS, В", "I_RMS, А", "PF", "Чтение"]
        for c, h in enumerate(headers):
            color = "#000080" if "Ref" in h else "black"
            ttk.Label(grid_f, text=h, font=("Arial", 9, "bold"), foreground=color).grid(row=0, column=c, padx=3, pady=2)

        self.cv_entries = []
        cv_labels = ["~ 50 В", "~ 100 В", "~ 140 В", "~ 200 В"]
        for r, label in enumerate(cv_labels, start=1):
            row_entries = []

            lbl = ttk.Label(grid_f, text=label, font=("Arial", 10, "bold"))
            lbl.grid(row=r, column=0, padx=5, pady=2)
            row_entries.append(lbl)

            for c in range(1, 11):
                style = "Ref.TEntry" if c in (1, 3, 5) else "Dev.TEntry"
                e = ttk.Entry(grid_f, width=8, style=style, justify="center")
                if c in (5, 6): e.config(state="readonly")
                e.grid(row=r, column=c, padx=2, pady=2)
                row_entries.append(e)
            
            btn = tk.Button(grid_f, text="R", width=2, fg="blue", bg="#f0f0f0", relief="raised", font=("Arial", 8, "bold"))
            btn.configure(command=lambda idx=r-1, b=btn: self.read_btn_click("CV", idx, b))
            self.all_read_buttons.append(btn)
            btn.grid(row=r, column=11, padx=5, pady=2)
            self.cv_entries.append(row_entries)
            
        bf = ttk.Frame(f); bf.pack(anchor="w", pady=6)
        ttk.Button(bf, text="Пересчитать", command=self.recalc_all).pack(side="left", padx=5)
        ttk.Button(bf, text="Очистить всё", command=self.clear_cv).pack(side="left", padx=5)
        
        tf, self.cv_text = make_text_with_scroll(f); tf.pack(fill="both", expand=True, pady=4)

    def clear_cv(self):
        for row in self.cv_entries:
            for c in [1, 2, 3, 4, 7, 8, 9, 10]: 
                row[c].config(state="normal"); row[c].delete(0, tk.END)
            row[5].config(state="normal"); row[5].delete(0, tk.END); row[5].config(state="readonly")
            row[6].config(state="normal"); row[6].delete(0, tk.END); row[6].config(state="readonly")
        self.recalc_all()

    def build_cc_tab(self):
        f = self.tab_cc
        ttk.Label(f, text="Выход CC (стабилизация тока)", font=("Arial", 11, "bold")).pack(anchor="w")
        ttk.Label(f, text="Введите Ref значения и нажмите [R] для считывания Dev.", foreground="#555").pack(anchor="w", pady=(0,10))
        
        grid_f = ttk.Frame(f); grid_f.pack(anchor="w")
        headers = ["Метка", "I_Lamp Ref, А", "I_Lamp, А", "V_Lamp Ref, В", "V_Lamp, В", "Pout Ref, Вт", "Pout, Вт", "Efficiency", "V_RMS, В", "I_RMS, А", "PF", "Чтение"]
        for c, h in enumerate(headers):
            color = "#000080" if "Ref" in h else "black"
            ttk.Label(grid_f, text=h, font=("Arial", 9, "bold"), foreground=color).grid(row=0, column=c, padx=3, pady=2)

        self.cc_entries = []
        cc_labels = ["~ 350 мА", "~ 700 мА", "~ 1400 мА"]
        for r, label in enumerate(cc_labels, start=1):
            row_entries = []
            lbl = ttk.Label(grid_f, text=label, font=("Arial", 10, "bold"))
            lbl.grid(row=r, column=0, padx=5, pady=2)
            row_entries.append(lbl)

            for c in range(1, 11):
                style = "Ref.TEntry" if c in (1, 3, 5) else "Dev.TEntry"
                e = ttk.Entry(grid_f, width=8, style=style, justify="center")
                if c in (5, 6): e.config(state="readonly")
                e.grid(row=r, column=c, padx=2, pady=2)
                row_entries.append(e)
                
            btn = tk.Button(grid_f, text="R", width=2, fg="blue", bg="#f0f0f0", relief="raised", font=("Arial", 8, "bold"))
            btn.configure(command=lambda idx=r-1, b=btn: self.read_btn_click("CC", idx, b))
            self.all_read_buttons.append(btn)
            btn.grid(row=r, column=11, padx=5, pady=2)
            self.cc_entries.append(row_entries)

        bf = ttk.Frame(f); bf.pack(anchor="w", pady=6)
        ttk.Button(bf, text="Пересчитать", command=self.recalc_all).pack(side="left", padx=5)
        ttk.Button(bf, text="Очистить всё", command=self.clear_cc).pack(side="left", padx=5)
        
        tf, self.cc_text = make_text_with_scroll(f); tf.pack(fill="both", expand=True, pady=4)

    def clear_cc(self):
        for row in self.cc_entries:
            for c in [1, 2, 3, 4, 7, 8, 9, 10]:
                row[c].config(state="normal"); row[c].delete(0, tk.END)
            row[5].config(state="normal"); row[5].delete(0, tk.END); row[5].config(state="readonly")
            row[6].config(state="normal"); row[6].delete(0, tk.END); row[6].config(state="readonly")
        self.recalc_all()

    def build_valid_tab(self):
        f = self.tab_valid
        info = ("Валидация проверяет отклонение прибора от эталона.\n"
                "Δ % (ошибка) — реальное отклонение прибора ДО калибровки.\n"
                "Δ % (ост) — остаточное отклонение ПОСЛЕ применения формулы (a * Dev + b).\n"
                "Допуск рассчитывается по формуле: % от показания + абсолютная погрешность.")
        ttk.Label(f, text=info, foreground="#555", font=("Arial", 9), justify="left").pack(anchor="w", pady=(0, 10))
        
        cols = ("point", "dev", "a", "b", "corr", "ref", "err_pct", "res_pct", "status")
        headers = ["Точка", "Dev", "a (наклон)", "b (смещ.)", "Corr (a*Dev+b)", "Ref", "Δ % (ошибка)", "Δ % (ост)", "Вердикт"]
        self.valid_tree = ttk.Treeview(f, columns=cols, show="headings", height=26)
        for c, h in zip(cols, headers):
            self.valid_tree.heading(c, text=h)
            self.valid_tree.column(c, width=110, anchor="center")
            
        self.valid_tree.tag_configure('pass', background='#d4edda', foreground='#155724')
        self.valid_tree.tag_configure('fail', background='#f8d7da', foreground='#721c24')
        self.valid_tree.tag_configure('header', background='#e0e0e0', foreground='#000000', font=('Arial', 9, 'bold'))
        self.valid_tree.tag_configure('nodata', background='#f0f0f0', foreground='#777')
        
        self.valid_tree.pack(fill="both", expand=True, pady=4)

    def build_instr_tab(self):
        tf, self.instr_text = make_text_with_scroll(self.tab_instr)
        tf.pack(fill="both", expand=True, pady=4)
        self.instr_text.configure(font=("Arial", 10), wrap="word")
        
        instr = (
            "================================================================================\n"
            "        ИНСТРУКЦИЯ ПО КАЛИБРОВКЕ UI2058F / UI2088F (МЕТОД ЛИНЕЙНОЙ РЕГРЕССИИ)\n"
            "================================================================================\n\n"
            "1. ФИЗИЧЕСКИЙ СМЫСЛ КОЭФФИЦИЕНТОВ (a и b)\n"
            "   Программа вычисляет поправочную характеристику прибора по уравнению прямой:\n"
            "       Ref = a * Dev + b\n"
            "   где:\n"
            "     • Dev — показание калибруемого прибора (считывается по кнопке [R]);\n"
            "     • Ref — показание эталонного мультиметра Agilent 34401A (вводится вручную);\n"
            "     • a (наклон / Gain) — мультипликативный коэффициент масштаба (в идеале = 1.0000);\n"
            "     • b (смещение / Offset) — аддитивная поправка смещения нуля (в идеале = 0.00).\n\n"
            "2. КАК ПРОГРАММА ВЫЧИСЛЯЕТ a И b (МЕТОД НАИМЕНЬШИХ КВАДРАТОВ)\n"
            "   Шаг 1. Фильтрация: из расчетов автоматически исключаются пустые или нулевые\n"
            "          точки (где Ref = 0 или Dev = 0), чтобы они не искажали прямую.\n"
            "          Для расчета необходимо минимум 2 заполненные точки на канал.\n"
            "   Шаг 2. Вычисление средних значений по всем валидным точкам:\n"
            "          X_ср = среднее(Dev),   Y_ср = среднее(Ref)\n"
            "   Шаг 3. Расчет коэффициентов:\n"
            "          a = Σ[(Dev_i - X_ср) * (Ref_i - Y_ср)] / Σ[(Dev_i - X_ср)²]\n"
            "          b = Y_ср - a * X_ср\n\n"
            "3. ПОРЯДОК ПРОВЕДЕНИЯ КАЛИБРОВКИ ПО ВКЛАДКАМ\n"
            "   • Вкладка 1 «Вход (12 точек)»: проверка входных каналов Uвх, Iвх, Pвх на\n"
            "     резистивной нагрузке (PF = 1.0). Введите Uref (В) и Iref (мА), нажмите [R].\n"
            "   • Вкладка 2 «Выход CV»: режим стабилизации напряжения (~50..200 В).\n"
            "     Введите V_Lamp Ref (В) и I_Lamp Ref (А), нажмите [R]. Pout считается сам.\n"
            "   • Вкладка 3 «Выход CC»: режим стабилизации тока (~350..1400 мА).\n"
            "     Введите I_Lamp Ref (А) и V_Lamp Ref (В), нажмите [R].\n\n"
            "4. АЛГОРИТМ ВАЛИДАЦИИ И ОТБРАКОВКИ (ВКЛАДКА 4)\n"
            "   Программа проверяет прибор по двум уровням защиты:\n\n"
            "   Уровень А — Контроль исправности «железа» (правая панель):\n"
            "     Если коэффициент наклона 'a' выходит за пределы 0.95 ... 1.05 (отклонение > 5%),\n"
            "     он подсвечивается КРАСНЫМ. Это признак аппаратного дефекта (неверный номинал\n"
            "     шунта или делителя напряжения), даже если программно прямая вытягивается.\n\n"
            "   Уровень Б — Проверка остаточной погрешности каждой точки:\n"
            "     1) Вычисляется скорректированное значение: Corr = a * Dev + b.\n"
            "     2) Считается остаточная ошибка: Δ % (ост) = (Corr - Ref) / Ref * 100%.\n"
            "     3) Ошибка сравнивается с комбинированным допуском (% от Ref + абс. смещение):\n"
            "          - Напряжение: ±(1.0% + 0.25 В)\n"
            "          - Ток:        ±(1.2% + 1.5 мА)\n"
            "          - Мощность:   ±(1.5% + 0.5 Вт)\n"
            "     Комбинированный допуск защищает от ложного брака на малых токах из-за\n"
            "     высокочастотных пульсаций импульсного LED-драйвера.\n"
            "     Если |Δ % (ост)| > Допуска, точка получает статус «БРАК», а конкретные\n"
            "     проблемные ячейки на рабочих вкладках подсвечиваются красным фоном.\n\n"
            "5. ЭКСПОРТ И ИМПОРТ В EXCEL\n"
            "   • «Сохранить в Excel»: создает книгу с 5 листами. На лист «0. Протокол»\n"
            "     выгружаются все 18 коэффициентов (a и b) для записи в память прибора.\n"
            "     На лист «4. Валидация (Годен)» попадают только точки, прошедшие допуск.\n"
            "   • «Загрузить из Excel»: восстанавливает таблицу измерений для доработки.\n"
        )
        self.instr_text.insert(tk.END, instr)

    # ============================================================
    # МАТЕМАТИКА И ОБНОВЛЕНИЕ
    # ============================================================
    def recalc_input(self):
        rows = []
        for g in self.in_entries:
            for i in range(3):
                ur = get_float(g["u"][i][0]); ud = get_float(g["u"][i][1])
                ir = get_float(g["i"][i][0]); id_ = get_float(g["i"][i][1])
                rows.append((ur, ud, ir, id_, 1.0, 1.0)) 
            
        r = calc_input(rows)
        self.in_result = r

        for g_idx, g in enumerate(self.in_entries):
            for i in range(3):
                idx = g_idx * 3 + i
                g["p"][i][0].config(text=f"{r['rows'][idx][4]:.3f}")
                g["p"][i][1].config(text=f"{r['rows'][idx][5]:.3f}")

        t = []
        t.append(f"{'№':>3} {'RefU':>9} {'DevU':>9} {'ΔU':>8} {'RefI':>9} {'DevI':>9} {'ΔI':>8} {'RefP':>10} {'DevP':>10} {'ΔP':>10}")
        t.append("-" * 105)
        for i, (ur, ud, ir_, id_, pr, pd, dU, dI, dP) in enumerate(r["rows"], 1):
            t.append(f"{i:>3} {ur:>9.3f} {ud:>9.3f} {dU:>+8.3f} {ir_:>9.4f} {id_:>9.4f} {dI:>+8.4f} {pr:>10.3f} {pd:>10.3f} {dP:>+10.3f}")
        self.in_text.delete("1.0", tk.END); self.in_text.insert(tk.END, "\n".join(t))

    def recalc_cv(self):
        rows = []
        for e in self.cv_entries:
            rows.append((
                e[0].cget("text"), 
                get_float(e[1]), get_float(e[2]), get_float(e[3]), get_float(e[4]), 
                get_float(e[8]), get_float(e[9]), get_float(e[10]), get_float(e[7]) 
            ))
            
        r = calc_cv(rows)
        self.cv_result = r
        
        for idx, row_data in enumerate(r["rows"]):
            e = self.cv_entries[idx]
            e[5].config(state="normal"); e[5].delete(0, tk.END); e[5].insert(0, f"{row_data['pout_ref']:.3f}"); e[5].config(state="readonly")
            e[6].config(state="normal"); e[6].delete(0, tk.END); e[6].insert(0, f"{row_data['pout_dev']:.3f}"); e[6].config(state="readonly")

        t = []
        t.append("КАЛИБРОВОЧНЫЕ КОЭФФИЦИЕНТЫ (CV):")
        t.append("-" * 40)
        t.append(f" a_V_Lamp = {r['k_VL']:.6f}, b_V_Lamp = {r['b_VL']:.6f}")
        t.append(f" a_I_Lamp = {r['k_IL']:.6f}, b_I_Lamp = {r['b_IL']:.6f}")
        t.append(f" a_Pout   = {r['k_P']:.6f},  b_Pout   = {r['b_P']:.6f}")
        t.append("-" * 40)
        for row in r["rows"]:
            t.append(f"── {row['label']} ──")
            t.append(f"  Pout Ref = {row['pout_ref']:.3f} Вт | Pout Dev = {row['pout_dev']:.3f} Вт")
            t.append(f"  Efficiency = {row['eta']:.4f}")
            t.append("")
        self.cv_text.delete("1.0", tk.END); self.cv_text.insert(tk.END, "\n".join(t))

    def recalc_cc(self):
        rows = []
        for e in self.cc_entries:
            rows.append((
                e[0].cget("text"), 
                get_float(e[1]), get_float(e[2]), get_float(e[3]), get_float(e[4]), 
                get_float(e[8]), get_float(e[9]), get_float(e[10]), get_float(e[7]) 
            ))
            
        r = calc_cc(rows)
        self.cc_result = r
        
        for idx, row_data in enumerate(r["rows"]):
            e = self.cc_entries[idx]
            e[5].config(state="normal"); e[5].delete(0, tk.END); e[5].insert(0, f"{row_data['pout_ref']:.3f}"); e[5].config(state="readonly")
            e[6].config(state="normal"); e[6].delete(0, tk.END); e[6].insert(0, f"{row_data['pout_dev']:.3f}"); e[6].config(state="readonly")

        t = []
        t.append("КАЛИБРОВОЧНЫЕ КОЭФФИЦИЕНТЫ (CC):")
        t.append("-" * 40)
        t.append(f" a_I_Lamp = {r['kIout']:.6f}, b_I_Lamp = {r['bIout']:.6f}")
        t.append(f" a_V_Lamp = {r['kVout']:.6f}, b_V_Lamp = {r['bVout']:.6f}")
        t.append(f" a_Pout   = {r['kPout']:.6f}, b_Pout   = {r['bPout']:.6f}")
        t.append("-" * 40)
        for row in r["rows"]:
            t.append(f"── {row['label']} ──")
            t.append(f"  ΔI_Lamp = {row['di']:+.4f} А")
            t.append(f"  Pout = {row['pout_dev']:.3f} Вт | Efficiency = {row['eta']:.4f}")
            t.append("")
        self.cc_text.delete("1.0", tk.END); self.cc_text.insert(tk.END, "\n".join(t))

    def update_valid(self):
        if not (hasattr(self, "in_result") and hasattr(self, "cv_result") and hasattr(self, "cc_result")): return
        for item in self.valid_tree.get_children(): self.valid_tree.delete(item)
        ir, cr, kr = self.in_result, self.cv_result, self.cc_result

        for g in self.in_entries:
            for i in range(3):
                g["u"][i][0].config(style="Ref.TEntry"); g["u"][i][1].config(style="Dev.TEntry")
                g["i"][i][0].config(style="Ref.TEntry"); g["i"][i][1].config(style="Dev.TEntry")
        for row in self.cv_entries:
            for c in range(1, 11):
                if c not in (5, 6): row[c].config(style="Ref.TEntry" if c in (1,3) else "Dev.TEntry")
            row[5].config(style="Ref.TEntry")
            row[6].config(style="Dev.TEntry")
        for row in self.cc_entries:
            for c in range(1, 11):
                if c not in (5, 6): row[c].config(style="Ref.TEntry" if c in (1,3) else "Dev.TEntry")
            row[5].config(style="Ref.TEntry")
            row[6].config(style="Dev.TEntry")

        self.valid_tree.insert("", "end", values=("--- 1. ВХОД (12 ТОЧЕК) ---", "", "", "", "", "", "", ""), tags=("header",))
        
        def get_limit(val_ref, param_type, unit="A"):
            val = abs(val_ref)
            if val == 0:
                return 0.0
            if param_type == "voltage":
                return TOL_U_PCT + (TOL_U_ABS / val) * 100.0
            elif param_type == "current":
                val_ma = val if unit == "mA" else val * 1000.0
                return TOL_I_MA_PCT + (TOL_I_MA_ABS / val_ma) * 100.0
            elif param_type == "power":
                return TOL_P_PCT + (TOL_P_ABS / val) * 100.0
            return 1.5

        def add_row(point_name, dev, a, b, ref, param_type, unit="A", widgets_to_highlight=None):
            if dev == 0.0 or ref == 0.0:
                self.valid_tree.insert("", "end", values=(point_name, f"{dev:.4f}", f"{a:.4f}", f"{b:.4f}", "-", f"{ref:.4f}", "-", "-", "НЕТ ДАННЫХ"), tags=("nodata",))
                return

            corr = a * dev + b
            err_dev = (dev - ref) / ref * 100
            err_corr = (corr - ref) / ref * 100
            
            limit = get_limit(ref, param_type, unit)
            status = "ГОДЕН" if abs(err_corr) <= limit else "БРАК"
            tag = "pass" if status == "ГОДЕН" else "fail"
            
            self.valid_tree.insert("", "end", values=(point_name, f"{dev:.4f}", f"{a:.4f}", f"{b:.4f}", f"{corr:.4f}", f"{ref:.4f}", f"{err_dev:+.3f}%", f"{err_corr:+.3f}%", status), tags=(tag,))

            if status == "БРАК" and widgets_to_highlight:
                for w in widgets_to_highlight:
                    w.config(style="DevFail.TEntry")

        for i, (ur, ud) in enumerate(zip(ir["uref"], ir["udev"]), 1): 
            g_idx = (i - 1) // 3; p_idx = (i - 1) % 3
            g = self.in_entries[g_idx]
            add_row(f"  Uвх (т.{i})", ud, ir["kU"], ir["bU"], ur, "voltage", "V", [g["u"][p_idx][0], g["u"][p_idx][1]])
        for i, (irf, idev) in enumerate(zip(ir["iref"], ir["idev"]), 1): 
            g_idx = (i - 1) // 3; p_idx = (i - 1) % 3
            g = self.in_entries[g_idx]
            add_row(f"  Iвх (т.{i})", idev, ir["kI"], ir["bI"], irf, "current", "mA", [g["i"][p_idx][0], g["i"][p_idx][1]])
        for i, (pr, pd) in enumerate(zip(ir["pref"], ir["pdev"]), 1):
            g_idx = (i - 1) // 3; p_idx = (i - 1) % 3
            g = self.in_entries[g_idx]
            add_row(f"  Pвх (т.{i})", pd, ir["kP"], ir["bP"], pr, "power", "W", [g["u"][p_idx][0], g["u"][p_idx][1], g["i"][p_idx][0], g["i"][p_idx][1]])

        self.valid_tree.insert("", "end", values=("--- 2. ВЫХОД CV ---", "", "", "", "", "", "", ""), tags=("header",))
        for idx, row in enumerate(cr["rows"]): 
            e = self.cv_entries[idx]
            add_row(f"  {row['label']} (V_Lamp)", row["vl_dev"], cr["k_VL"], cr["b_VL"], row["vl_ref"], "voltage", "V", [e[1], e[2]])
            add_row(f"  {row['label']} (I_Lamp)", row["il_dev"], cr["k_IL"], cr["b_IL"], row["il_ref"], "current", "A", [e[3], e[4]])
            add_row(f"  {row['label']} (Pout)", row["pout_dev"], cr["k_P"], cr["b_P"], row["pout_ref"], "power", "W", [e[5], e[6]])
        
        self.valid_tree.insert("", "end", values=("--- 3. ВЫХОД CC ---", "", "", "", "", "", "", ""), tags=("header",))
        for idx, row in enumerate(kr["rows"]): 
            e = self.cc_entries[idx]
            add_row(f"  {row['label']} (I_Lamp)", row["idev"], kr["kIout"], kr["bIout"], row["iref"], "current", "A", [e[1], e[2]])
            add_row(f"  {row['label']} (V_Lamp)", row["uout_dev"], kr["kVout"], kr["bVout"], row["uout"], "voltage", "V", [e[3], e[4]])
            add_row(f"  {row['label']} (Pout)", row["pout_dev"], kr["kPout"], kr["bPout"], row["pout_ref"], "power", "W", [e[5], e[6]])

    def recalc_all(self):
        self.recalc_input()
        self.recalc_cv()
        self.recalc_cc()
        self.update_valid()
        
        ir, cr, kr = self.in_result, self.cv_result, self.cc_result
        
        def set_coef_lbl(key, a_val, b_val):
            lbl = self.coef_labels[key]
            lbl.config(text=f"a={a_val:.4f} b={b_val:.2f}")
            if abs(a_val - 1.0) > MAX_GAIN_DEV:
                lbl.config(foreground="#D32F2F")
            else:
                lbl.config(foreground="#000080")

        set_coef_lbl("kU", ir['kU'], ir['bU'])
        set_coef_lbl("kI", ir['kI'], ir['bI'])
        set_coef_lbl("kP", ir['kP'], ir['bP'])
        
        set_coef_lbl("kVL", cr['k_VL'], cr['b_VL'])
        set_coef_lbl("kIL", cr['k_IL'], cr['b_IL'])
        set_coef_lbl("kPoutCV", cr['k_P'], cr['b_P'])
        
        set_coef_lbl("kICC", kr['kIout'], kr['bIout'])
        set_coef_lbl("kVC", kr['kVout'], kr['bVout'])
        set_coef_lbl("kPoutCC", kr['kPout'], kr['bPout'])
        
        self.status.set("Все данные пересчитаны.")

    # ============================================================
    # СОХРАНЕНИЕ И ЗАГРУЗКА В EXCEL
    # ============================================================
    def save_excel(self):
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")])
        if not path:
            return

        try:
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
            from openpyxl.utils import get_column_letter

            ir, cr, kr = self.in_result, self.cv_result, self.cc_result
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            dev_type = self.ent_dev_type.get().strip() or "Не указан"
            dev_sn = self.ent_dev_sn.get().strip() or "Не указан"

            # Создаем чистую книгу openpyxl напрямую
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = '0. Протокол'

            # Стили
            font_title = Font(name="Calibri", size=14, bold=True, color="000080")
            font_sec = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
            font_hdr = Font(name="Calibri", size=10, bold=True, color="000000")
            font_bold = Font(name="Calibri", size=10, bold=True)
            font_reg = Font(name="Calibri", size=10)
            font_code = Font(name="Consolas", size=9, color="333333")

            fill_sec = PatternFill(start_color="2F5597", end_color="2F5597", fill_type="solid")
            fill_hdr = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
            fill_meta = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
            fill_input = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
            fill_res = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")

            thin_side = Side(style='thin', color='BFBFBF')
            border_cell = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

            # --- ШАПКА ПРОТОКОЛА ---
            ws.merge_cells("A1:H1")
            ws["A1"] = "ПРОТОКОЛ КАЛИБРОВКИ ИЗМЕРИТЕЛЬНОГО ПРИБОРА"
            ws["A1"].font = font_title
            ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
            ws.row_dimensions[1].height = 28

            meta_rows = [
                ("Тип прибора:", dev_type, "Дата и время калибровки:", now_str),
                ("Серийный номер (S/N):", dev_sn, "Эталонный прибор:", "Agilent 34401A")
            ]
            for idx, (k1, v1, k2, v2) in enumerate(meta_rows, start=3):
                ws[f"A{idx}"] = k1; ws[f"A{idx}"].font = font_bold; ws[f"A{idx}"].fill = fill_meta
                ws[f"B{idx}"] = v1; ws[f"B{idx}"].font = font_reg
                ws[f"D{idx}"] = k2; ws[f"D{idx}"].font = font_bold; ws[f"D{idx}"].fill = fill_meta
                ws[f"E{idx}"] = v2; ws[f"E{idx}"].font = font_reg
                for col in ["A", "B", "D", "E"]:
                    ws[f"{col}{idx}"].border = border_cell

            sections = [
                ("1. ВХОДНЫЕ ПАРАМЕТРЫ (СЕТЬ AC)", [
                    ("Входное напряжение (U_вх), В", "a_U_in", round(ir['kU'], 6), "b_U_in", round(ir['bU'], 6)),
                    ("Входной ток (I_вх), мА", "a_I_in", round(ir['kI'], 6), "b_I_in", round(ir['bI'], 6)),
                    ("Входная активная мощность (P_вх), Вт", "a_P_in", round(ir['kP'], 6), "b_P_in", round(ir['bP'], 6)),
                ]),
                ("2. ВЫХОДНЫЕ ПАРАМЕТРЫ — РЕЖИМ CV (СТАБИЛИЗАЦИЯ НАПРЯЖЕНИЯ)", [
                    ("Выходное напряжение (V_Lamp), В", "a_V_Lamp (CV)", round(cr['k_VL'], 6), "b_V_Lamp (CV)", round(cr['b_VL'], 6)),
                    ("Выходной ток (I_Lamp), А", "a_I_Lamp (CV)", round(cr['k_IL'], 6), "b_I_Lamp (CV)", round(cr['b_IL'], 6)),
                    ("Выходная мощность (Pout), Вт", "a_Pout (CV)", round(cr['k_P'], 6), "b_Pout (CV)", round(cr['b_P'], 6)),
                ]),
                ("3. ВЫХОДНЫЕ ПАРАМЕТРЫ — РЕЖИМ CC (СТАБИЛИЗАЦИЯ ТОКА)", [
                    ("Выходной ток (I_Lamp), А", "a_I_Lamp (CC)", round(kr['kIout'], 6), "b_I_Lamp (CC)", round(kr['bIout'], 6)),
                    ("Выходное напряжение (V_Lamp), В", "a_V_Lamp (CC)", round(kr['kVout'], 6), "b_V_Lamp (CC)", round(kr['bVout'], 6)),
                    ("Выходная мощность (Pout), Вт", "a_Pout (CC)", round(kr['kPout'], 6), "b_Pout (CC)", round(kr['bPout'], 6)),
                ])
            ]

            headers = [
                "Измеряемый параметр (ед. изм.)",
                "Код коэф. a", "Значение a (наклон)",
                "Код коэф. b", "Значение b (смещ.)",
                "Формула пересчета",
                "Dev_раб (ввести)",
                "Corr_раб (результат)"
            ]

            curr_row = 6
            for sec_title, items in sections:
                ws.merge_cells(start_row=curr_row, start_column=1, end_row=curr_row, end_column=8)
                cell_sec = ws.cell(row=curr_row, column=1, value=sec_title)
                cell_sec.font = font_sec
                cell_sec.fill = fill_sec
                cell_sec.alignment = Alignment(horizontal="left", vertical="center", indent=1)
                ws.row_dimensions[curr_row].height = 22
                curr_row += 1

                for c_idx, h_text in enumerate(headers, start=1):
                    c = ws.cell(row=curr_row, column=c_idx, value=h_text)
                    c.font = font_hdr
                    c.fill = fill_hdr
                    c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                    c.border = border_cell
                ws.row_dimensions[curr_row].height = 24
                curr_row += 1

                for p_name, code_a, val_a, code_b, val_b in items:
                    sign = "+" if val_b >= 0 else "-"
                    full_formula_str = f"Corr = Dev_раб * {val_a:.6f} {sign} {abs(val_b):.6f}"

                    ws.cell(row=curr_row, column=1, value=p_name).font = font_bold
                    ws.cell(row=curr_row, column=2, value=code_a).font = font_code
                    ws.cell(row=curr_row, column=3, value=val_a).font = font_reg
                    ws.cell(row=curr_row, column=4, value=code_b).font = font_code
                    ws.cell(row=curr_row, column=5, value=val_b).font = font_reg
                    ws.cell(row=curr_row, column=6, value=full_formula_str).font = font_code

                    c_in = ws.cell(row=curr_row, column=7, value=None)
                    c_in.fill = fill_input
                    c_in.font = font_bold

                    c_out = ws.cell(row=curr_row, column=8, value=f'=IF(G{curr_row}="","",ROUND(G{curr_row}*C{curr_row}+E{curr_row},4))')
                    c_out.fill = fill_res
                    c_out.font = font_bold

                    for col_i in range(1, 9):
                        cell = ws.cell(row=curr_row, column=col_i)
                        cell.border = border_cell
                        if col_i in (2, 3, 4, 5, 7, 8):
                            cell.alignment = Alignment(horizontal="center", vertical="center")
                        else:
                            cell.alignment = Alignment(horizontal="left", vertical="center")
                    ws.row_dimensions[curr_row].height = 20
                    curr_row += 1
                curr_row += 1

            col_widths = [38, 16, 18, 16, 18, 34, 18, 22]
            for i, width in enumerate(col_widths, start=1):
                ws.column_dimensions[get_column_letter(i)].width = width

            def write_df_to_sheet(sheet_name, df):
                s = wb.create_sheet(title=sheet_name)
                if df.empty: return
                s.append(list(df.columns))
                for cell in s[1]:
                    cell.font = font_hdr
                    cell.fill = fill_hdr
                    cell.alignment = Alignment(horizontal="center")
                for row_vals in df.itertuples(index=False):
                    s.append(list(row_vals))
                for col_idx in range(1, len(df.columns) + 1):
                    s.column_dimensions[get_column_letter(col_idx)].width = 16

            # Лист 1: Вход
            input_data = []
            for g_idx, g in enumerate(self.in_entries):
                for i in range(3):
                    input_data.append({
                        "Группа P": f"P~{g_idx*50+7} Вт" if g_idx == 0 else f"P~{g_idx*50} Вт",
                        "Uref": g["u"][i][0].get(), "Udev": g["u"][i][1].get(),
                        "Iref": g["i"][i][0].get(), "Idev": g["i"][i][1].get(),
                        "Pref": g["p"][i][0].cget("text"), "Pdev": g["p"][i][1].cget("text")
                    })
            write_df_to_sheet('1. Вход', pd.DataFrame(input_data))

            # Лист 2: Выход CV
            cv_data = []
            for row in self.cv_entries:
                cv_data.append({
                    "Метка": row[0].cget("text"),
                    "V_Lamp Ref": row[1].get(), "V_Lamp Dev": row[2].get(),
                    "I_Lamp Ref": row[3].get(), "I_Lamp Dev": row[4].get(),
                    "Pout Ref": row[5].get(), "Pout Dev": row[6].get(),
                    "Efficiency": row[7].get(),
                    "V_RMS": row[8].get(), "I_RMS": row[9].get(), "PF": row[10].get()
                })
            write_df_to_sheet('2. Выход CV', pd.DataFrame(cv_data))

            # Лист 3: Выход CC
            cc_data = []
            for row in self.cc_entries:
                cc_data.append({
                    "Метка": row[0].cget("text"),
                    "I_Lamp Ref": row[1].get(), "I_Lamp Dev": row[2].get(),
                    "V_Lamp Ref": row[3].get(), "V_Lamp Dev": row[4].get(),
                    "Pout Ref": row[5].get(), "Pout Dev": row[6].get(),
                    "Efficiency": row[7].get(),
                    "V_RMS": row[8].get(), "I_RMS": row[9].get(), "PF": row[10].get()
                })
            write_df_to_sheet('3. Выход CC', pd.DataFrame(cc_data))

            # Лист 4: Валидация
            valid_data = []
            for item in self.valid_tree.get_children():
                vals = self.valid_tree.item(item, "values")
                if len(vals) > 0 and "---" not in vals[0] and vals[8] == "ГОДЕН":
                    valid_data.append({
                        "Точка": vals[0], "Dev": vals[1], "a": vals[2], "b": vals[3], "corr": vals[4],
                        "Ref": vals[5], "Δ % (ошибка)": vals[6], "Δ % (ост)": vals[7], "Вердикт": vals[8]
                    })
            if valid_data:
                write_df_to_sheet('4. Валидация (Годен)', pd.DataFrame(valid_data))

            # Лист 5: Инструкция
            ws_instr = wb.create_sheet(title='5. Инструкция')
            instr_content = self.instr_text.get("1.0", tk.END).splitlines()
            for r_idx, line in enumerate(instr_content, start=1):
                # Защита от интерпретации '===' как формулы в Excel/LibreOffice (Ошибка:510).
                # Строки, начинающиеся с '=', — это декоративные разделители.
                # Заменяем ВСЕ '=' на '-' в таких строках, чтобы табличный
                # редактор не пытался их вычислить.
                clean_line = line.replace("=", "-") if line.lstrip().startswith("=") else line

                c = ws_instr.cell(row=r_idx, column=1, value=clean_line)
                if "----" in clean_line or (len(clean_line) > 2 and clean_line[0].isdigit() and clean_line[1] == "."):
                    c.font = font_bold
                else:
                    c.font = Font(name="Consolas", size=10)
            ws_instr.column_dimensions['A'].width = 95

            wb.save(path)
            self.status.set(f"Протокол сохранен: {path}")
            messagebox.showinfo("Успех", "Оформленный протокол и инструкция сохранены в Excel.")

        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить файл:\n{e}")

    def load_excel(self):
        path = filedialog.askopenfilename(filetypes=[("Excel", "*.xlsx")])
        if not path: return
        try:
            xls = pd.ExcelFile(path)
            
            if '0. Протокол' in xls.sheet_names:
                df_prot = pd.read_excel(xls, sheet_name='0. Протокол', header=None)
                dev_t, dev_s = "", ""
                if df_prot.shape[0] >= 4 and df_prot.shape[1] >= 2:
                    cell_a1 = str(df_prot.iloc[0, 0]).strip()
                    if "ПРОТОКОЛ" in cell_a1:
                        dev_t = str(df_prot.iloc[2, 1]) if pd.notna(df_prot.iloc[2, 1]) else ""
                        dev_s = str(df_prot.iloc[3, 1]) if pd.notna(df_prot.iloc[3, 1]) else ""
                    elif cell_a1 == "Параметр":
                        prot_dict = dict(zip(df_prot[0], df_prot[1]))
                        dev_t = str(prot_dict.get("Тип прибора", ""))
                        dev_s = str(prot_dict.get("S/N", ""))
                    
                    self.ent_dev_type.delete(0, tk.END); self.ent_dev_type.insert(0, dev_t)
                    self.ent_dev_sn.delete(0, tk.END); self.ent_dev_sn.insert(0, dev_s)

            if '1. Вход' in xls.sheet_names:
                df_in = pd.read_excel(xls, sheet_name='1. Вход')
                for idx, row in df_in.iterrows():
                    if idx >= 12: break
                    g_idx = idx // 3; p_idx = idx % 3
                    g = self.in_entries[g_idx]
                    
                    def insert_val(widget, val):
                        widget.delete(0, tk.END)
                        if pd.notna(val) and str(val).strip() != "" and str(val).lower() != "nan":
                            widget.insert(0, str(val))
                    
                    insert_val(g["u"][p_idx][0], row.get("Uref"))
                    insert_val(g["u"][p_idx][1], row.get("Udev"))
                    insert_val(g["i"][p_idx][0], row.get("Iref"))
                    insert_val(g["i"][p_idx][1], row.get("Idev"))

            if '2. Выход CV' in xls.sheet_names:
                df_cv = pd.read_excel(xls, sheet_name='2. Выход CV')
                for idx, row in df_cv.iterrows():
                    if idx >= len(self.cv_entries): break
                    e = self.cv_entries[idx]
                    for c, col_name in zip([1,2,3,4,7,8,9,10], ["V_Lamp Ref", "V_Lamp Dev", "I_Lamp Ref", "I_Lamp Dev", "Efficiency", "V_RMS", "I_RMS", "PF"]):
                        e[c].delete(0, tk.END)
                        val = row.get(col_name)
                        if pd.notna(val) and str(val).strip() != "" and str(val).lower() != "nan":
                            e[c].insert(0, str(val))

            if '3. Выход CC' in xls.sheet_names:
                df_cc = pd.read_excel(xls, sheet_name='3. Выход CC')
                for idx, row in df_cc.iterrows():
                    if idx >= len(self.cc_entries): break
                    e = self.cc_entries[idx]
                    for c, col_name in zip([1,2,3,4,7,8,9,10], ["I_Lamp Ref", "I_Lamp Dev", "V_Lamp Ref", "V_Lamp Dev", "Efficiency", "V_RMS", "I_RMS", "PF"]):
                        e[c].delete(0, tk.END)
                        val = row.get(col_name)
                        if pd.notna(val) and str(val).strip() != "" and str(val).lower() != "nan":
                            e[c].insert(0, str(val))

            self.recalc_all()
            self.status.set(f"Загружено из Excel: {path}")
            messagebox.showinfo("Успех", "Данные успешно загружены.")

        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось загрузить файл:\n{e}")

if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
