import streamlit as st
import zipfile
import re
import pandas as pd
import io
import math
from datetime import datetime, timedelta

# --- SAYFA AYARLARI VE CSS ---
st.set_page_config(page_title="KUKA Backup Analyzer PRO", page_icon="🤖", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
    <style>
    .reportview-container { background: #f0f2f6; }
    div[data-testid="metric-container"] {
        background-color: #ffffff; border: 1px solid #e0e4e8; padding: 15px 20px;
        border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.05);
        border-left: 5px solid #f39c12; transition: transform 0.2s ease;
    }
    div[data-testid="metric-container"]:hover { transform: translateY(-2px); box-shadow: 0 6px 12px rgba(0,0,0,0.1); }
    thead tr th { background-color: #34495e !important; color: white !important; font-weight: bold !important; }
    .main-header {
        background: linear-gradient(135deg, #2c3e50, #3498db); padding: 20px;
        border-radius: 10px; color: white; margin-bottom: 25px; box-shadow: 0 4px 15px rgba(0,0,0,0.1);
    }
    .main-header h1 { margin: 0; font-size: 32px; color: #ffffff; }
    </style>
""", unsafe_allow_html=True)

# --- GLOBAL VERİ DEPOSU ---
if 'parsed_data' not in st.session_state:
    st.session_state.parsed_data = None

# --- PARSER FONKSİYONLARI ---
def parse_values(val_str):
    res = {"X": 0.0, "Y": 0.0, "Z": 0.0, "A": 0.0, "B": 0.0, "C": 0.0, "M": 0.0}
    for p in val_str.split(','):
        if '=' in p:
            kv = p.split('=')
            try: res[kv[0].strip().upper()] = float(kv[1].strip())
            except: pass
    return res

def parse_ct_logs_line(line, logs_list):
    if not line.lower().strip().startswith("ct_log["): return
    inner_match = re.search(r'\{([^}]+)\}', line)
    if not inner_match: return
    
    d = {}
    for part in inner_match.group(1).split(','):
        part = part.strip()
        space_idx = part.find(' ')
        eq_idx = part.find('=')
        if space_idx != -1 and (eq_idx == -1 or space_idx < eq_idx):
            d[part[:space_idx].strip().upper()] = part[space_idx+1:].strip().replace('"', '')
        elif eq_idx != -1:
            d[part[:eq_idx].strip().upper()] = part[eq_idx+1:].strip().replace('"', '')

    if "TARIH" in d and "ZAMAN" in d:
        try:
            t_str, z = d["TARIH"], str(d["ZAMAN"]).zfill(6)
            y = int(t_str[0:4]) if len(t_str) >= 8 else 2000 + int(t_str[0:2])
            m = int(t_str[4:6]) if len(t_str) >= 8 else int(t_str[2:4])
            day = int(t_str[6:8]) if len(t_str) >= 8 else int(t_str[4:6])
            dt = datetime(y, m, day, int(z[0:2]), int(z[2:4]), int(z[4:6]))
            
            logs_list.append({
                "Tarih_Obj": dt,
                "Tarih": dt.strftime("%Y-%m-%d"),
                "Bitiş Zamanı": dt.strftime("%H:%M:%S"),
                "Operasyon": int(d.get("OPERASYON", d.get("OP", d.get("OPCODE", 0)))),
                "Fikstür": int(d.get("TIP2", d.get("FIX", d.get("FIXTURE", 0)))),
                "Çevrim (sn)": float(d.get("CEVRIMZAMANI", d.get("CEVRIM", d.get("CYCLE", 0)))),
                "Yükleme (sn)": float(d.get("LOADOP", d.get("YUKLEME", 0))),
                "Kaynak (sn)": float(d.get("KAYNAKSURESI", d.get("KAYNAK", 0)))
            })
        except: pass

def extract_dat_info(dat_content):
    points = {}
    wdata_info = {}
    
    for match in re.finditer(r"DECL\s+(?:E6POS|POS)\s+([\w_]+)\s*=\s*\{([^}]+)\}", dat_content, re.IGNORECASE):
        p_name = match.group(1).upper()
        p_val = match.group(2)
        x_m, y_m, z_m = re.search(r"X\s+([-\d\.eE]+)", p_val, re.IGNORECASE), re.search(r"Y\s+([-\d\.eE]+)", p_val, re.IGNORECASE), re.search(r"Z\s+([-\d\.eE]+)", p_val, re.IGNORECASE)
        if x_m:
            coords = {'x': float(x_m.group(1)), 'y': float(y_m.group(1)) if y_m else 0.0, 'z': float(z_m.group(1)) if z_m else 0.0}
            points[p_name] = coords
            points[p_name[1:] if p_name.startswith('X') else 'X' + p_name] = coords

    for match in re.finditer(r"DECL\s+[\w_]+\s+((?:WDATA|WDAT)[\w_]*)\s*=\s*\{", dat_content, re.IGNORECASE):
        w_name = match.group(1).upper()
        block, bc, i = "", 1, match.end()
        while bc > 0 and i < len(dat_content):
            if dat_content[i] == '{': bc += 1
            if dat_content[i] == '}': bc -= 1
            if bc > 0: block += dat_content[i]
            i += 1
            
        vel, job = "-", 0
        w_m = re.search(r"Weld\s*\{([^}]+)\}", block, re.IGNORECASE)
        if w_m:
            inner = w_m.group(1)
            v_m = re.search(r"Velocity\s+([-\d\.eE]+)", inner, re.IGNORECASE)
            j_m = re.search(r"Channel1\s+([-\d\.]+)", inner, re.IGNORECASE)
            if v_m: vel = float(v_m.group(1))
            if j_m: job = int(float(j_m.group(1)))
        wdata_info[w_name] = {'job': job, 'velocity': vel}
        
    return points, wdata_info

def parse_backup(file):
    config_dat, longtexts_dat = "", ""
    bases, tools, signals, logs = [], [], {}, []
    programs = {}
    
    with zipfile.ZipFile(file, 'r') as z:
        for filename in z.namelist():
            if z.getinfo(filename).is_dir(): continue
            lower_name = filename.lower().replace('\\', '/')
            ext = lower_name.split('.')[-1]
            
            if lower_name.endswith('$config.dat') and ('krc/r1/system' in lower_name or '/' not in lower_name):
                config_dat = z.read(filename).decode('utf-8', errors='ignore')
            elif 'longtexts' in lower_name and ext in ['bak', 'txt', 'csv']:
                longtexts_dat += z.read(filename).decode('utf-8', errors='ignore') + "\n"
            elif ext in ['src', 'dat']:
                content = z.read(filename).decode('utf-8', errors='ignore')
                if ext == 'dat':
                    for line in content.splitlines(): parse_ct_logs_line(line, logs)
                
                bare_name = re.sub(r'\.(src|dat)$', '', lower_name.split('/')[-1], flags=re.IGNORECASE).upper()
                if bare_name not in programs: programs[bare_name] = {'src': "", 'dat': ""}
                programs[bare_name][ext] = content

    if not config_dat: return None

    # Base ve Tool Ayıklama
    b_data = dict(re.findall(r"(?:\$)?BASE_DATA\[(\d+)\]\s*=\s*\{([^}]+)\}", config_dat, re.IGNORECASE))
    b_name = dict(re.findall(r"(?:\$)?BASE_NAME\[(\d+),\s*\]\s*=\s*\"([^\"]*)\"", config_dat, re.IGNORECASE))
    for idx, data_str in b_data.items():
        if idx in b_name and b_name[idx].strip():
            bases.append({"Base No": int(idx), "Base Adı": b_name[idx], **parse_values(data_str)})

    t_data = dict(re.findall(r"(?:\$)?TOOL_DATA\[(\d+)\]\s*=\s*\{([^}]+)\}", config_dat, re.IGNORECASE))
    t_name = dict(re.findall(r"(?:\$)?TOOL_NAME\[(\d+),\s*\]\s*=\s*\"([^\"]*)\"", config_dat, re.IGNORECASE))
    l_data = dict(re.findall(r"(?:\$)?LOAD_DATA\[(\d+)\]\s*=\s*\{([^}]+)\}", config_dat, re.IGNORECASE))
    for idx, data_str in t_data.items():
        if idx in t_name and t_name[idx].strip():
            mass = parse_values(l_data.get(idx, "")).get("M", 0.0)
            tools.append({"Tool No": int(idx), "Tool Adı": t_name[idx], "Yük (kg)": mass, **parse_values(data_str)})

    # Sinyal Haritalama
    if longtexts_dat:
        for line in longtexts_dat.splitlines():
            parts = line.split(';')
            if len(parts) >= 2:
                addr_part, name_part = parts[0].strip().upper(), ';'.join(parts[1:]).strip()
                if not name_part or name_part.upper() == 'SPARE': continue
                s_type, addr = "", ""
                if addr_part.startswith('$IN['): s_type, addr = "INPUT", addr_part[4:-1]
                elif addr_part.startswith('$OUT['): s_type, addr = "OUTPUT", addr_part[5:-1]
                elif addr_part.startswith('$TIMER['): s_type, addr = "TIMER", addr_part[7:-1]
                elif addr_part.startswith('$FLAG['): s_type, addr = "FLAG", addr_part[6:-1]
                if s_type: signals[f"{s_type}_{addr}"] = {"Tip": s_type, "Sinyal Adı": name_part, "Adres": addr}

    for match in re.findall(r"SIGNAL\s+([A-Za-z0-9_]+)\s+(\$IN|\$OUT)\[(\d+)\]", config_dat, re.IGNORECASE):
        key = f"{'INPUT' if match[1].upper() == '$IN' else 'OUTPUT'}_{match[2]}"
        if key in signals: signals[key]["Sinyal Adı"] += f" [{match[0]}]"
        else: signals[key] = {"Tip": "INPUT" if match[1].upper() == "$IN" else "OUTPUT", "Sinyal Adı": match[0], "Adres": match[2]}

    # Log İşleme ve Smart Best / Duruş(Gap) Algoritması
    df_logs = pd.DataFrame(logs)
    if not df_logs.empty:
        df_logs.sort_values(by="Tarih_Obj", inplace=True)
        df_logs.reset_index(drop=True, inplace=True)
        
        # HATA DÜZELTİLDİ: Grup sütunu önce eklenmeli!
        df_logs['Grup'] = df_logs['Tarih'] + "_" + df_logs['Fikstür'].astype(str)
        
        # Smart Best Sapma
        valid_logs = df_logs[df_logs["Çevrim (sn)"] > 10]
        if not valid_logs.empty:
            bests = valid_logs.groupby('Grup')['Çevrim (sn)'].quantile(0.10).to_dict()
            df_logs['Sapma (sn)'] = df_logs.apply(lambda row: max(0, row['Çevrim (sn)'] - bests.get(row['Grup'], row['Çevrim (sn)'])), axis=1)
            bests_load = valid_logs.groupby('Grup')['Yükleme (sn)'].quantile(0.10).to_dict()
            df_logs['Yükleme Sapma'] = df_logs.apply(lambda row: max(0, row['Yükleme (sn)'] - bests_load.get(row['Grup'], row['Yükleme (sn)'])), axis=1)
        else:
            df_logs['Sapma (sn)'], df_logs['Yükleme Sapma'] = 0.0, 0.0

        # Başlangıç ve Gap (Duruş) Hesabı
        gaps, gap_types, start_times = [0.0], ["Start"], [df_logs.at[0, "Tarih_Obj"] - timedelta(seconds=df_logs.at[0, "Çevrim (sn)"])]
        for i in range(1, len(df_logs)):
            cur_dt, cur_cev = df_logs.at[i, "Tarih_Obj"], df_logs.at[i, "Çevrim (sn)"]
            cur_start = cur_dt - timedelta(seconds=cur_cev)
            start_times.append(cur_start)
            prev_dt = df_logs.at[i-1, "Tarih_Obj"]
            raw_gap = (cur_start - prev_dt).total_seconds()
            
            if raw_gap > 14400:
                gaps.append(0.0); gap_types.append("OffShift")
            else:
                l_s, l_e = prev_dt.replace(hour=12, minute=40, second=0), prev_dt.replace(hour=13, minute=20, second=0)
                o_s, o_e = max(prev_dt, l_s), min(cur_start, l_e)
                o_sec = (o_e - o_s).total_seconds() if o_e > o_s else 0.0
                gap = max(0.0, raw_gap - o_sec)
                gaps.append(gap)
                gap_types.append("Lunch+Delay" if o_sec > 0 and gap >= 10 else ("Lunch" if o_sec > 0 else "Normal"))

        df_logs['Başlangıç Zamanı'] = [st.strftime("%H:%M:%S") for st in start_times]
        df_logs['Duruş (Gap)'] = gaps
        df_logs['Duruş Tipi'] = gap_types
        df_logs = df_logs[['Tarih', 'Başlangıç Zamanı', 'Bitiş Zamanı', 'Operasyon', 'Fikstür', 'Çevrim (sn)', 'Sapma (sn)', 'Yükleme (sn)', 'Yükleme Sapma', 'Kaynak (sn)', 'Duruş (Gap)', 'Duruş Tipi']]

    return {"bases": pd.DataFrame(bases), "tools": pd.DataFrame(tools), "signals": pd.DataFrame(list(signals.values())), "logs": df_logs, "programs": programs}

def calculate_welding(programs, wire_dia, mat_den, job_mapping_str):
    welding_seams = []
    job_speeds = {}
    for p in job_mapping_str.split(','):
        pr = p.split(':')
        if len(pr) == 2:
            try: job_speeds[int(pr[0].strip())] = float(pr[1].strip())
            except: pass

    for p_name, f in programs.items():
        if not f['src'] or not f['dat']: continue
        pts, wd_info = extract_dat_info(f['dat'])
        lines = f['src'].splitlines()
        
        in_w, c_w, l_pt = False, {}, None
        for line in lines:
            l_u = line.strip().upper()
            if 'ARCON' in l_u and 'FOLD' in l_u:
                w_m = re.search(r"(WDATA[\w_]*|WDAT[\w_]*)", l_u)
                m_m = re.search(r"(LIN|PTP|CIRC)\s+([A-Za-z_][\w_]*)", l_u)
                w_name = w_m.group(1) if w_m else ""
                info = wd_info.get(w_name, {'job': 0, 'velocity': '-'})
                start_pt = m_m.group(2) if m_m else "UNKNOWN"
                job = info['job']
                
                c_w = {
                    'Program': p_name, 'Başlangıç': start_pt, 'Bitiş': '...', 'Mesafe (mm)': 0.0,
                    'Hız': info['velocity'], 'Job': job, 'Tel Hızı (m/dk)': job_speeds.get(job, 0.0),
                    'Süre (sn)': 0.0, 'Tel Tük. (m)': 0.0, 'Ağırlık (kg)': 0.0
                }
                l_pt = pts.get(start_pt)
                in_w = True
                continue
                
            if in_w and 'ARCOFF' in l_u and 'FOLD' in l_u:
                m_m = re.search(r"(LIN|PTP|CIRC)\s+([A-Za-z_][\w_]*)", l_u)
                end_pt = m_m.group(2) if m_m else "UNKNOWN"
                c_w['Bitiş'] = end_pt
                
                if l_pt and end_pt in pts:
                    p = pts[end_pt]
                    c_w['Mesafe (mm)'] += math.sqrt((p['x']-l_pt['x'])**2 + (p['y']-l_pt['y'])**2 + (p['z']-l_pt['z'])**2)
                
                try:
                    v_v = float(c_w['Hız'])
                    if v_v > 0: c_w['Süre (sn)'] = c_w['Mesafe (mm)'] / (v_v * 1000.0)
                except: pass
                
                if c_w['Tel Hızı (m/dk)'] > 0:
                    c_w['Tel Tük. (m)'] = (c_w['Süre (sn)'] / 60.0) * c_w['Tel Hızı (m/dk)']
                    c_w['Ağırlık (kg)'] = (math.pi * ((wire_dia/2.0)**2) * mat_den * 0.001) * c_w['Tel Tük. (m)']
                    
                welding_seams.append(c_w)
                in_w, c_w, l_pt = False, {}, None
                continue
                
            if in_w and not l_u.startswith(';'):
                m_m = re.search(r"(LIN|PTP|CIRC)\s+([A-Za-z_][\w_]*)", l_u)
                if m_m and m_m.group(2) in pts:
                    p = pts[m_m.group(2)]
                    if l_pt: c_w['Mesafe (mm)'] += math.sqrt((p['x']-l_pt['x'])**2 + (p['y']-l_pt['y'])**2 + (p['z']-l_pt['z'])**2)
                    l_pt = p

    return pd.DataFrame(welding_seams)

# --- YAN MENÜ TASARIMI ---
with st.sidebar:
    st.markdown("<h2 style='text-align: center; color: #f39c12; margin-bottom: 0;'>🤖 KUKA PRO</h2>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: #94a3b8; font-size: 13px; margin-top: 0;'>Endüstriyel Veri Analizi</p>", unsafe_allow_html=True)
    st.markdown("---")
    menu = st.radio("Navigasyon", ("📊 Özet Dashboard", "🕒 Üretim Kayıtları (ct_log)", "🔥 Kaynak Analizi", "📦 Base (Frame) Verileri", "🎯 Tool (TCP) Verileri", "⚡ I/O Sinyal Haritası"))
    st.markdown("---")

# --- ANA EKRAN ---
st.markdown("""<div class="main-header"><h1>KUKA Backup Analyzer PRO</h1><p>Robot yedeklerinizi analiz edin, kaynak tüketimini ve döngü sapmalarını tespit edin.</p></div>""", unsafe_allow_html=True)

col1, col2 = st.columns([3, 1])
with col1:
    uploaded_file = st.file_uploader("📥 Analize başlamak için KUKA .zip yedeğini yükleyin", type="zip")

if uploaded_file is not None:
    if st.session_state.parsed_data is None:
        with st.spinner("🚀 Dosyalar taranıyor ve veriler ayıklanıyor..."):
            st.session_state.parsed_data = parse_backup(uploaded_file)
            if st.session_state.parsed_data is None:
                st.error("❌ $config.dat bulunamadı!")
                st.stop()
            st.success("✅ Analiz başarıyla tamamlandı!")

    data = st.session_state.parsed_data

    with col2:
        st.markdown("<br>", unsafe_allow_html=True)
        # Raporlama indirilebilir hale de getirilebilir isteğe göre.
        st.button("📊 Raporu İndir (Çok Yakında)", disabled=True) 

    st.markdown("---")

    if menu == "📊 Özet Dashboard":
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("📌 Toplam Üretim Logu", len(data['logs']))
        c2.metric("Taranan Program", len(data['programs']))
        c3.metric("📦 Tanımlı Base", len(data['bases']))
        c4.metric("🎯 Tanımlı Tool", len(data['tools']))

    elif menu == "🕒 Üretim Kayıtları (ct_log)":
        st.subheader("Üretim Kayıtları (Smart Best & Gap Algoritması ile)")
        if not data['logs'].empty:
            styled_logs = data['logs'].style.format({"Çevrim (sn)": "{:.2f}", "Sapma (sn)": "{:.2f}", "Yükleme (sn)": "{:.2f}", "Yükleme Sapma": "{:.2f}", "Duruş (Gap)": "{:.2f}"})\
                .map(lambda x: 'background-color: #ffcccc; color: #900;' if x > 5 else '', subset=['Sapma (sn)', 'Duruş (Gap)'])
            st.dataframe(styled_logs, use_container_width=True, height=600)
        else: st.warning("Bu yedekte üretim (ct_log) verisi bulunamadı.")

    elif menu == "🔥 Kaynak Analizi":
        st.subheader("Kaynak Analizi Hesaplayıcısı")
        col_w1, col_w2, col_w3 = st.columns(3)
        wire_dia = col_w1.number_input("Tel Çapı (mm)", value=1.0, step=0.1)
        mat_den_str = col_w2.selectbox("Malzeme", ["Çelik/SM-70 (7.85)", "Paslanmaz (7.9)", "Alüminyum (2.7)"])
        mat_den = float(mat_den_str.split('(')[1].replace(')',''))
        job_map = col_w3.text_input("Job Hızları (JobNo: Metre/Dk)", "1:12, 2:10, 3:8")
        
        with st.spinner("Kaynak rotaları ve dikişleri hesaplanıyor..."):
            df_weld = calculate_welding(data['programs'], wire_dia, mat_den, job_map)
            if not df_weld.empty:
                st.dataframe(df_weld.style.format({"Mesafe (mm)": "{:.2f}", "Süre (sn)": "{:.2f}", "Tel Tük. (m)": "{:.3f}", "Ağırlık (kg)": "{:.4f}"}), use_container_width=True, height=500)
                cw1, cw2, cw3, cw4 = st.columns(4)
                cw1.metric("Toplam Kaynak Mesafesi", f"{df_weld['Mesafe (mm)'].sum():.2f} mm")
                cw2.metric("Toplam Kaynak Süresi", f"{df_weld['Süre (sn)'].sum():.2f} sn")
                cw3.metric("Toplam Tel Tüketimi", f"{df_weld['Tel Tük. (m)'].sum():.3f} m")
                cw4.metric("Toplam Tel Ağırlığı", f"{df_weld['Ağırlık (kg)'].sum():.4f} kg")
            else: st.warning("Bu yedekte kaynak (ARCON/ARCOFF) verisi bulunamadı.")

    elif menu == "📦 Base (Frame) Verileri":
        st.dataframe(data['bases'], use_container_width=True, height=600)

    elif menu == "🎯 Tool (TCP) Verileri":
        st.dataframe(data['tools'], use_container_width=True, height=600)

    elif menu == "⚡ I/O Sinyal Haritası":
        st.dataframe(data['signals'], use_container_width=True, height=600)