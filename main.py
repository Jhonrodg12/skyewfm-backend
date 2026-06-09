"""
API del optimizador WFM — backend para Render.
Expone un endpoint POST /generar-roster que:
  1. Lee el histórico (CSV en Supabase Storage o enviado en la petición)
  2. Corre el optimizador (motor.py) + repartidor (repartidor.py)
  3. Genera breaks por país
  4. Escribe asignaciones + breaks en Supabase

Seguridad: protegido por una API key secreta (header X-API-Key).
"""
import os
import io
from datetime import datetime, timedelta
from collections import defaultdict

import pandas as pd
from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text

import motor
import repartidor

app = FastAPI(title="WFM Optimizer API")

# CORS: permitir que skyewfm.com llame a esta API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://skyewfm.com", "https://www.skyewfm.com"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Credenciales desde variables de entorno (se configuran en Render) ---
DB_URL = os.environ.get("DB_URL")            # cadena de conexión del pooler de Supabase
API_KEY = os.environ.get("API_KEY")          # clave secreta para proteger el endpoint

LIBRES = {p: {p, (p + 1) % 7} for p in range(7)}


def get_engine():
    if not DB_URL:
        raise HTTPException(500, "DB_URL no configurada en el servidor")
    return create_engine(DB_URL, pool_pre_ping=True)


def check_key(x_api_key):
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(401, "API key inválida")


# ---------- breaks por país ----------
def _t(hhmm):
    h, m = map(int, str(hhmm).split(":")[:2])
    return datetime(2000, 1, 1, h % 24, m)


def breaks_colombia(hi, dur, idx, ventana=30):
    ini = _t(hi); off = timedelta(minutes=(idx * 5) % ventana); r = []
    r.append(((ini + timedelta(hours=2) + off).strftime("%H:%M"), 10, "break"))
    r.append(((ini + timedelta(hours=dur / 2) + off).strftime("%H:%M"), 30, "almuerzo"))
    r.append(((ini + timedelta(hours=dur - 2) + off).strftime("%H:%M"), 10, "break"))
    return r


def breaks_espana(hi, dur, idx, ventana=30):
    ini = _t(hi); off = timedelta(minutes=(idx * 5) % ventana); r = []
    r.append(((ini + timedelta(hours=dur / 2) + off).strftime("%H:%M"), 30, "descanso"))
    for h in range(int(dur)):
        r.append(((ini + timedelta(hours=h, minutes=50)).strftime("%H:%M"), 5, "break"))
    return r


def dur_turno(hi, hf):
    a = _t(hi); b = _t(hf); d = (b - a).seconds / 3600
    return d if d > 0 else d + 24


@app.get("/")
def health():
    return {"status": "ok", "service": "WFM Optimizer API"}


@app.get("/capacidad-anual")
def capacidad_anual(
    x_api_key: str = Header(None),
    anio: int = 2026,
    scope: str = "Nacional España",
    K: int = 4,
    aht: int = 420, sla: float = 0.80, asa: int = 20, occ: float = 0.65,
    utl: float = 0.88, esp_max: int = 12, largo: int = 9,
    nda_obj: float = 0.96, paciencia: int = 90,
    estructura: str = "mixto", absentismo: float = 0.15,
    plantilla_actual: int = 129,
):
    """Dimensiona los 12 meses del año: agentes necesarios por mes."""
    check_key(x_api_key)
    import math as _m
    engine = get_engine()
    file_bytes = _historico_a_csv_bytes(engine)
    # fechas con datos reales (para marcar estado)
    dch = pd.read_sql(text("SELECT DISTINCT fecha FROM historico_llamadas"), engine)
    dias_datos = set(pd.to_datetime(dch["fecha"]).dt.normalize())

    MESES = ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"]
    filas = []
    for mth in range(1, 13):
        mes_str = f"{anio}-{mth:02d}"
        ini = pd.Timestamp(anio, mth, 1); dm = pd.date_range(ini, ini + pd.offsets.MonthEnd(0))
        nr = sum(1 for t in dm if t in dias_datos)
        estado = "Real" if nr >= len(dm) else ("En curso" if nr > 0 else "Proyectado")
        try:
            largo_df = motor.largo_desde_historico(file_bytes, mes_str, scope, K, 6, 0.0, mixto=False)
            Sx = motor.dimension_roster(largo_df, aht, sla, asa, occ, utl, esp_max, largo,
                                        nda_obj, paciencia, estructura)
            presentes = Sx["te"] + Sx["tc"]
            nomina = _m.ceil(presentes / (1 - absentismo)) if absentismo < 1 else presentes
            filas.append({"mes": MESES[mth-1], "estado": estado, "volumen": Sx["total"],
                          "espana": Sx["te"], "colombia": Sx["tc"], "presentes": presentes,
                          "en_nomina": nomina})
        except Exception:
            filas.append({"mes": MESES[mth-1], "estado": estado, "volumen": 0,
                          "espana": 0, "colombia": 0, "presentes": 0, "en_nomina": 0})

    validos = [f for f in filas if f["en_nomina"] > 0]
    pico = max(validos, key=lambda f: f["en_nomina"]) if validos else None
    prom = round(sum(f["en_nomina"] for f in validos) / len(validos)) if validos else 0
    return {
        "ok": True, "anio": anio,
        "tabla": filas,
        "mes_pico": {"mes": pico["mes"], "en_nomina": pico["en_nomina"]} if pico else None,
        "promedio_nomina": prom,
        "plantilla_actual": plantilla_actual,
    }


@app.get("/proyeccion-anual")
def proyeccion_anual(
    x_api_key: str = Header(None),
    anio: int = None,
    K: int = 4,
    recencia: float = 0.0,
    scope: str = "Nacional España",
    colas: str = None,
    ajuste_meses: str = None,   # 12 valores % separados por coma, ej "0,0,10,0,..."
):
    """Proyección anual de volumen: Base (sin ajustes) vs Escenario (con ajustes)."""
    check_key(x_api_key)
    import holidays as _hol
    engine = get_engine()
    d = pd.read_sql(text("SELECT fecha, cola, entrantes FROM historico_llamadas"), engine)
    if d.empty:
        return {"ok": True, "vacio": True}
    d["fecha"] = pd.to_datetime(d["fecha"]).dt.normalize()
    d["entrantes"] = pd.to_numeric(d["entrantes"], errors="coerce").fillna(0)

    share = d.groupby("cola")["entrantes"].sum().sort_values(ascending=False)
    share_pct = (share / share.sum() * 100).round(1)

    colas_sel = [c.strip() for c in colas.split(",")] if colas else list(share.index)
    colas_sel = [c for c in colas_sel if c in share.index] or list(share.index)
    d = d[d["cola"].isin(colas_sel)]

    dia = d.groupby("fecha")["entrantes"].sum()
    if dia.empty:
        return {"ok": True, "vacio": False, "sin_datos": True}
    anios_disp = sorted({t.year for t in dia.index})
    if anio is None:
        anio = anios_disp[-1]

    adj_mes = [0.0] * 12
    if ajuste_meses:
        parts = [p.strip() for p in ajuste_meses.split(",")]
        for i, p in enumerate(parts[:12]):
            try: adj_mes[i] = float(p) / 100.0
            except: adj_mes[i] = 0.0

    # peso por cola (sin ajuste de cola individual por ahora; el ajuste va por mes)
    cola_factor = 1.0

    if "Catal" in scope:
        ES = _hol.Spain(years=range(min(anios_disp) - 1, max(anios_disp) + 2), subdiv="CT")
    else:
        ES = _hol.Spain(years=range(min(anios_disp) - 1, max(anios_disp) + 2))

    def fest(t): return t.date() in ES

    idx_dow = dia.index.dayofweek
    def proj_dia(t):
        wd = 6 if fest(t) else t.dayofweek
        s = dia[(dia.index < t) & (idx_dow == wd)]
        if wd != 6:
            s = s[[not fest(x) for x in s.index]]
        s = s.tail(K)
        if len(s) == 0: return 0.0
        v = s.values[::-1]
        import numpy as _np
        w = _np.array([(1 - recencia) ** j for j in range(len(v))])
        return float((v * w).sum() / w.sum())

    MESES = ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"]
    filas = []
    for mth in range(1, 13):
        ini = pd.Timestamp(year=anio, month=mth, day=1); fin = ini + pd.offsets.MonthEnd(0)
        dias_mes = pd.date_range(ini, fin)
        real_part = pbase = 0.0
        for t in dias_mes:
            if t in dia.index: real_part += float(dia.loc[t])
            else: pbase += proj_dia(t)
        n_real = sum(1 for t in dias_mes if t in dia.index)
        estado = "REAL" if n_real >= len(dias_mes) else ("EN CURSO" if n_real > 0 else "PROYECTADO")
        f_mes = cola_factor * (1 + adj_mes[mth - 1])
        base_tot = real_part + pbase
        scn_tot = real_part + pbase * f_mes
        delta = (scn_tot / base_tot - 1) * 100 if base_tot > 0 else 0.0
        filas.append({"mes": MESES[mth-1], "estado": estado, "real": int(round(real_part)),
                      "base": int(round(base_tot)), "escenario": int(round(scn_tot)), "delta": round(delta,1)})

    # Promedios históricos
    pdow = dia.groupby(idx_dow).mean().reindex(range(7)).fillna(0)
    NOM7 = ["Lun","Mar","Mié","Jue","Vie","Sáb","Dom"]
    prom_dow = [{"dia": NOM7[i], "promedio": round(float(pdow.values[i]),1)} for i in range(7)]
    dia_anio = dia[dia.index.year == anio]
    sem = dia_anio.resample("W").sum()
    por_semana = [{"semana": idx.strftime("%Y-%m-%d"), "entrantes": int(v)} for idx, v in sem.items()]

    tb = sum(f["base"] for f in filas); tsc = sum(f["escenario"] for f in filas)
    return {
        "ok": True, "vacio": False,
        "anio": anio, "anios_disponibles": anios_disp,
        "colas_disponibles": [{"cola": c, "pct": float(share_pct[c])} for c in share.index],
        "tabla": filas,
        "totales": {"base": tb, "escenario": tsc, "diferencia": tsc - tb,
                    "delta_pct": round((tsc/tb - 1)*100, 1) if tb else None},
        "promedio_dow": prom_dow,
        "por_semana": por_semana,
    }


@app.get("/dashboard-historico")
def dashboard_historico(
    x_api_key: str = Header(None),
    anio: int = None,
    mes: int = None,
    semana: int = None,
    colas: str = None,   # colas separadas por coma; vacío = todas
    dows: str = None,    # días de semana 0-6 separados por coma; vacío = todos
):
    """Devuelve datos agregados del histórico para el dashboard, según filtros."""
    check_key(x_api_key)
    engine = get_engine()
    df = pd.read_sql(text("SELECT fecha, hora, cola, entrantes, atendidas, abandonadas FROM historico_llamadas"), engine)
    if df.empty:
        return {"ok": True, "vacio": True}
    df["fecha"] = pd.to_datetime(df["fecha"])
    df["anio"] = df["fecha"].dt.year
    df["mes"] = df["fecha"].dt.month
    df["semana"] = df["fecha"].dt.isocalendar().week.astype(int)
    df["dow"] = df["fecha"].dt.dayofweek

    if anio:
        df = df[df["anio"] == anio]
    if mes:
        df = df[df["mes"] == mes]
    if semana:
        df = df[df["semana"] == semana]
    if colas:
        lista = [c.strip() for c in colas.split(",") if c.strip()]
        if lista:
            df = df[df["cola"].isin(lista)]
    if dows:
        lista_d = [int(x) for x in dows.split(",") if x.strip().isdigit()]
        if lista_d:
            df = df[df["dow"].isin(lista_d)]

    if df.empty:
        return {"ok": True, "vacio": False, "sin_datos_filtro": True}

    ent = int(df["entrantes"].sum()); at = int(df["atendidas"].sum()); ab = int(df["abandonadas"].sum())

    # Por cola
    by = df.groupby("cola")[["entrantes", "atendidas", "abandonadas"]].sum().reset_index()
    by["pat"] = (by["atendidas"] / by["entrantes"].replace(0, pd.NA) * 100).round(1)
    by = by.sort_values("entrantes", ascending=False)
    por_cola = by.to_dict("records")

    # % atención en el tiempo (por fecha)
    serie = df.groupby(df["fecha"].dt.strftime("%Y-%m-%d"))[["entrantes", "atendidas"]].sum()
    serie["pat"] = (serie["atendidas"] / serie["entrantes"].replace(0, pd.NA) * 100).round(1)
    en_tiempo = [{"fecha": idx, "pat": (None if pd.isna(row["pat"]) else float(row["pat"]))}
                 for idx, row in serie.iterrows()]

    # Por franja horaria
    fr = df.groupby("hora")[["entrantes", "atendidas", "abandonadas"]].sum()
    fr["pat"] = (fr["atendidas"] / fr["entrantes"].replace(0, pd.NA) * 100).round(1)
    por_hora = [{"hora": int(h), "entrantes": int(row["entrantes"]),
                 "pat": (None if pd.isna(row["pat"]) else float(row["pat"]))}
                for h, row in fr.iterrows()]

    return {
        "ok": True, "vacio": False,
        "metricas": {"entrantes": ent, "atendidas": at, "abandonadas": ab,
                     "pct_atencion": round(at / ent * 100, 1) if ent else None},
        "por_cola": por_cola,
        "en_tiempo": en_tiempo,
        "por_hora": por_hora,
    }


@app.get("/dashboard-opciones")
def dashboard_opciones(x_api_key: str = Header(None)):
    """Devuelve los valores disponibles para los filtros (años, colas)."""
    check_key(x_api_key)
    engine = get_engine()
    df = pd.read_sql(text("SELECT DISTINCT fecha, cola FROM historico_llamadas"), engine)
    if df.empty:
        return {"ok": True, "anios": [], "colas": []}
    df["fecha"] = pd.to_datetime(df["fecha"])
    anios = sorted(df["fecha"].dt.year.unique().tolist())
    colas = sorted(df["cola"].unique().tolist())
    return {"ok": True, "anios": anios, "colas": colas}


@app.post("/historico/actualizar")
async def historico_actualizar(
    x_api_key: str = Header(None),
    archivo: UploadFile = File(...),
):
    """
    Sube un CSV y lo fusiona con el histórico acumulado (upsert):
    - Filas con fecha+hora+cola ya existentes: se SOBRESCRIBEN.
    - Filas nuevas: se AGREGAN.
    Columnas esperadas: fecha, hora, cola, entrantes, atendidas, abandonadas.
    """
    check_key(x_api_key)
    engine = get_engine()
    file_bytes = await archivo.read()
    df = pd.read_csv(io.BytesIO(file_bytes))
    df.columns = [c.strip().lower() for c in df.columns]
    req = {"fecha", "hora", "cola", "entrantes", "atendidas", "abandonadas"}
    falta = req - set(df.columns)
    if falta:
        raise HTTPException(400, f"Faltan columnas en el CSV: {falta}")
    df = df[list(req)].copy()
    df["fecha"] = pd.to_datetime(df["fecha"]).dt.date
    for c in ["hora", "entrantes", "atendidas", "abandonadas"]:
        df[c] = df[c].astype(int)

    filas_csv = len(df)
    # upsert por lotes usando ON CONFLICT
    insertadas = 0
    with engine.begin() as conn:
        for _, r in df.iterrows():
            conn.execute(text("""
                INSERT INTO historico_llamadas (fecha, hora, cola, entrantes, atendidas, abandonadas, actualizado_en)
                VALUES (:f, :h, :c, :e, :a, :ab, now())
                ON CONFLICT (fecha, hora, cola)
                DO UPDATE SET entrantes=EXCLUDED.entrantes, atendidas=EXCLUDED.atendidas,
                              abandonadas=EXCLUDED.abandonadas, actualizado_en=now()
            """), {"f": r["fecha"], "h": int(r["hora"]), "c": str(r["cola"]),
                   "e": int(r["entrantes"]), "a": int(r["atendidas"]), "ab": int(r["abandonadas"])})
            insertadas += 1

    # rango del histórico tras la actualización
    rango = pd.read_sql(text("SELECT MIN(fecha) AS desde, MAX(fecha) AS hasta, COUNT(*) AS filas FROM historico_llamadas"), engine)
    return {
        "ok": True,
        "filas_csv": filas_csv,
        "filas_procesadas": insertadas,
        "historico_desde": str(rango["desde"][0]),
        "historico_hasta": str(rango["hasta"][0]),
        "total_filas_historico": int(rango["filas"][0]),
    }


@app.get("/historico/estado")
def historico_estado(x_api_key: str = Header(None)):
    """Devuelve hasta qué fecha hay histórico cargado."""
    check_key(x_api_key)
    engine = get_engine()
    rango = pd.read_sql(text("SELECT MIN(fecha) AS desde, MAX(fecha) AS hasta, COUNT(*) AS filas FROM historico_llamadas"), engine)
    if rango["filas"][0] == 0:
        return {"ok": True, "vacio": True}
    return {
        "ok": True, "vacio": False,
        "historico_desde": str(rango["desde"][0]),
        "historico_hasta": str(rango["hasta"][0]),
        "total_filas": int(rango["filas"][0]),
    }


def _historico_a_csv_bytes(engine):
    """Lee todo el histórico de la base y lo devuelve como CSV en bytes (para el optimizador)."""
    df = pd.read_sql(text("SELECT fecha, hora, cola, entrantes, atendidas, abandonadas FROM historico_llamadas ORDER BY fecha, hora"), engine)
    return df.to_csv(index=False).encode()


@app.post("/planeacion")
async def planeacion(
    x_api_key: str = Header(None),
    mes: str = Form(...),
    archivo: UploadFile = File(None),
    aht: int = Form(420),
    sla: float = Form(0.80),
    asa: int = Form(20),
    occ: float = Form(0.65),
    utl: float = Form(0.88),
    esp_max: int = Form(12),
    largo: int = Form(9),
    nda_obj: float = Form(0.96),
    paciencia: int = Form(90),
    estructura: str = Form("mixto"),
    absentismo: float = Form(0.15),
):
    """Corre el optimizador y devuelve datos para graficar SIN escribir en la base."""
    check_key(x_api_key)
    if archivo is not None:
        file_bytes = await archivo.read()
    else:
        file_bytes = _historico_a_csv_bytes(get_engine())
    largo_df = motor.largo_desde_historico(file_bytes, mes, "Nacional España", 4, 6, 0.0, mixto=False)
    S = motor.dimension_roster(largo_df, aht, sla, asa, occ, utl, esp_max, largo,
                               nda_obj, paciencia, estructura)
    turnos = motor.turnos_dict(S, largo)

    # Datos por día (0=Lun .. 6=Dom): requerido, programado, ocupación, NDA
    NOM = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    por_dia = []
    for d in range(7):
        req = [S["peak"][d][h] for h in range(24)]
        cob = [motor.cubierto(S, turnos, d, h) for h in range(24)]
        occv = [round(S["occ"][d][h] * 100, 1) for h in range(24)]
        ndav = [round(S["nda"][d][h] * 100, 1) for h in range(24)]
        por_dia.append({"dia": d, "nombre": NOM[d], "requerido": req,
                        "programado": cob, "ocupacion": occv, "nda": ndav})

    # Plan de turnos (tabla)
    plan = []
    for k in sorted(S["xe"], key=int):
        t = int(k)
        plan.append({"pais": "España", "inicio": f"{t:02d}:00",
                     "fin": f"{(t+largo)%24:02d}:00", "cantidad": S["xe"][k],
                     "libres": "Sáb, Dom"})
    for k in sorted(S["xc"], key=lambda x: (int(x.split("_")[0]), int(x.split("_")[1]))):
        t, p = map(int, k.split("_"))
        o = sorted(motor.LIBRES_VIZ[p])
        plan.append({"pais": "Colombia", "inicio": f"{t:02d}:00",
                     "fin": f"{(t+largo)%24:02d}:00", "cantidad": S["xc"][k],
                     "libres": f"{NOM[o[0]][:3]}, {NOM[o[1]][:3]}"})

    import math as _m
    presentes = S["te"] + S["tc"]
    en_nomina = _m.ceil(presentes / (1 - absentismo)) if absentismo < 1 else presentes

    return {
        "ok": True,
        "mes": mes,
        "volumen_total": S["total"],
        "estructura": estructura,
        "plantilla": {
            "espana_lv": S["te"],
            "colombia_247": S["tc"],
            "total_presentes": presentes,
            "en_nomina": en_nomina,
        },
        "por_dia": por_dia,
        "plan_turnos": plan,
        "objetivos": {"occ": round(occ * 100, 1), "nda": round(nda_obj * 100, 1)},
    }


@app.post("/generar-roster")
async def generar_roster(
    x_api_key: str = Header(None),
    mes: str = Form(...),                 # "2026-07"
    campana: str = Form("Endesa"),
    archivo: UploadFile = File(None),     # historico.csv (opcional: si no, usa histórico de base)
    aht: int = Form(420),
    sla: float = Form(0.80),
    asa: int = Form(20),
    occ: float = Form(0.65),
    utl: float = Form(0.88),
    esp_max: int = Form(12),
    largo: int = Form(9),
    nda_obj: float = Form(0.96),
    paciencia: int = Form(90),
    estructura: str = Form("mixto"),
):
    check_key(x_api_key)
    engine = get_engine()

    # 1) Leer histórico (del CSV subido o del histórico acumulado en la base) y correr optimizador
    if archivo is not None:
        file_bytes = await archivo.read()
    else:
        file_bytes = _historico_a_csv_bytes(engine)
    largo_df = motor.largo_desde_historico(file_bytes, mes, "Nacional España", 4, 6, 0.0, mixto=False)
    S = motor.dimension_roster(largo_df, aht, sla, asa, occ, utl, esp_max, largo,
                               nda_obj, paciencia, estructura)

    # 2) Repartir personas
    id_campana = pd.read_sql(text("SELECT id FROM campanas WHERE nombre=:n"),
                             engine, params={"n": campana})["id"][0]

    dias_mes = pd.date_range(f"{mes}-01", pd.Timestamp(f"{mes}-01") + pd.offsets.MonthEnd(0))

    # --- BLOQUEOS: asignaciones que el WFM marcó como fijas y NO deben recalcularse ---
    bloq = pd.read_sql(text("""
        SELECT agente_id, fecha FROM asignaciones
        WHERE campana_id=:c AND fecha BETWEEN :i AND :f AND bloqueado = true
    """), engine, params={"c": int(id_campana), "i": dias_mes[0].date(), "f": dias_mes[-1].date()})
    # set de (agente_id, fecha) bloqueados — esos días no se regeneran
    dias_bloqueados = set((int(r["agente_id"]), pd.Timestamp(r["fecha"]).date()) for _, r in bloq.iterrows())
    # agentes con TODO el mes bloqueado salen del reparto automático
    cont_bloq = bloq.groupby("agente_id").size() if not bloq.empty else pd.Series(dtype=int)
    agentes_full_bloq = set(int(a) for a, n in cont_bloq.items() if n >= len(dias_mes))

    # Solo agentes MULTISKILL reciben turno del optimizador (backoffice se asigna aparte)
    ag = pd.read_sql("SELECT id,nombre,centro,pais,jornada_horas,modo FROM agentes WHERE estado='ACTIVO' AND UPPER(modo)='MULTISKILL' ORDER BY id", engine)
    ag = ag[~ag["id"].isin(agentes_full_bloq)]  # excluir agentes totalmente bloqueados
    esp = ag[ag["pais"] == "España"].to_dict("records")
    col = ag[ag["pais"] == "Colombia"].to_dict("records")
    # Perfil de tráfico por hora (turnos que pidió el optimizador) — define dónde arrancan más agentes
    col_t, esp_t = repartidor.demanda_diaria(S)
    # Repartir TODOS los Multiskill (sin backup), según el perfil de tráfico
    base_col, res_col = repartidor.repartir_todos(col, col_t, set(range(7)), "Colombia", True)
    base_esp, res_esp = repartidor.repartir_todos(esp, esp_t, {0, 1, 2, 3, 4}, "España", False)
    base_todos = base_col + base_esp

    # 3) Registrar turnos usados
    horas_usadas = sorted({a["hora_inicio"] for a in base_todos})
    turno_id_por_hora = {}
    with engine.begin() as conn:
        for h in horas_usadas:
            hora_str = f"{h:02d}:00"
            row = conn.execute(text("SELECT id FROM turnos WHERE hora_inicio=:hi AND duracion_horas=:du LIMIT 1"),
                               {"hi": hora_str, "du": largo}).fetchone()
            if row:
                turno_id_por_hora[h] = row[0]
            else:
                r = conn.execute(text("INSERT INTO turnos (nombre,hora_inicio,duracion_horas,descripcion) VALUES (:n,:hi,:du,:de) RETURNING id"),
                                 {"n": f"Turno {hora_str} ({largo}h)", "hi": hora_str, "du": largo, "de": f"Optimizador {mes}"}).fetchone()
                turno_id_por_hora[h] = r[0]

    # 4) Generar asignaciones (saltando los días bloqueados por el WFM)
    filas = []
    for item in base_todos:
        a = item["agente"]; hi = item["hora_inicio"]; libres = item["libres"]
        hora_fin = (hi + largo) % 24; tid = turno_id_por_hora[hi]
        for d in dias_mes:
            # si ese día de ese agente está bloqueado, no lo regeneramos (se conserva el existente)
            if (int(a["id"]), d.date()) in dias_bloqueados:
                continue
            if d.dayofweek in libres:
                filas.append({"agente_id": int(a["id"]), "fecha": d.date(), "campana_id": int(id_campana),
                              "turno_id": None, "hora_inicio": None, "hora_fin": None, "tipo": "libre", "creado_por": "optimizador-web"})
            else:
                filas.append({"agente_id": int(a["id"]), "fecha": d.date(), "campana_id": int(id_campana),
                              "turno_id": int(tid), "hora_inicio": f"{hi:02d}:00", "hora_fin": f"{hora_fin:02d}:00", "tipo": "trabajo", "creado_por": "optimizador-web"})
    n_asig = len(filas)

    # 5) Cargar asignaciones (limpiar mes antes, PERO conservando las bloqueadas)
    #    INSERT MULTI-FILA con ON CONFLICT: mete muchas filas por sentencia (rápido),
    #    y si una (agente_id, fecha) ya existe, la actualiza SOLO si no está bloqueada.
    #    Así nunca choca con la restricción única y respeta los turnos bloqueados por el WFM.
    try:
        # DELETE de las no-bloqueadas
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM asignaciones WHERE campana_id=:c AND fecha>=:i AND fecha<=:f AND (bloqueado IS NULL OR bloqueado = false)"),
                         {"c": int(id_campana), "i": dias_mes[0].date(), "f": dias_mes[-1].date()})

        cols = ["agente_id", "fecha", "campana_id", "turno_id", "hora_inicio", "hora_fin", "tipo", "creado_por"]
        conflict = """
            ON CONFLICT (agente_id, fecha) DO UPDATE SET
                campana_id = EXCLUDED.campana_id,
                turno_id = EXCLUDED.turno_id,
                hora_inicio = EXCLUDED.hora_inicio,
                hora_fin = EXCLUDED.hora_fin,
                tipo = EXCLUDED.tipo,
                creado_por = EXCLUDED.creado_por
            WHERE asignaciones.bloqueado IS NOT TRUE
        """
        LOTE = 500
        with engine.begin() as conn:
            for k in range(0, len(filas), LOTE):
                lote = filas[k:k+LOTE]
                valores = []
                params = {}
                for j, f in enumerate(lote):
                    valores.append(f"(:agente_id{j}, :fecha{j}, :campana_id{j}, :turno_id{j}, :hora_inicio{j}, :hora_fin{j}, :tipo{j}, :creado_por{j})")
                    for col in cols:
                        params[f"{col}{j}"] = f[col]
                sql = "INSERT INTO asignaciones (" + ", ".join(cols) + ") VALUES " + ", ".join(valores) + conflict
                conn.execute(text(sql), params)
    except Exception as e:
        ejemplo = filas[0] if filas else {}
        raise HTTPException(500, f"Error al escribir asignaciones: {type(e).__name__}: {str(e)[:300]} | ejemplo fila: {ejemplo}")

    # 6) Los breaks se generan en un endpoint aparte (/generar-breaks) para que esta
    #    respuesta sea rápida y no exceda el límite de tiempo de Cloudflare/Lovable.

    return {
        "ok": True,
        "mes": mes,
        "volumen_total": S["total"],
        "asignaciones": n_asig,
        "breaks": "pendiente",
        "nota": "Asignaciones generadas. Los breaks se generan en el paso siguiente.",
        "colombia": res_col,
        "espana": res_esp,
    }


@app.post("/generar-breaks")
def generar_breaks(
    x_api_key: str = Header(None),
    mes: str = Form(...),
    campana: str = Form("Endesa"),
):
    """Genera los breaks del mes a partir de las asignaciones ya creadas.
    Se llama DESPUÉS de /generar-roster para repartir el trabajo y no exceder el tiempo límite."""
    check_key(x_api_key)
    engine = get_engine()
    id_campana = pd.read_sql(text("SELECT id FROM campanas WHERE nombre=:n"),
                             engine, params={"n": campana})["id"][0]
    dias_mes = pd.date_range(f"{mes}-01", pd.Timestamp(f"{mes}-01") + pd.offsets.MonthEnd(0))

    asg = pd.read_sql(text("""
        SELECT a.id AS asignacion_id, a.hora_inicio, a.hora_fin, ag.pais
        FROM asignaciones a JOIN agentes ag ON ag.id=a.agente_id
        WHERE a.campana_id=:c AND a.tipo='trabajo' AND a.fecha BETWEEN :i AND :f
        ORDER BY a.fecha, a.hora_inicio, a.id
    """), engine, params={"c": int(id_campana), "i": dias_mes[0].date(), "f": dias_mes[-1].date()})

    filas_b = []; contador = defaultdict(int)
    for _, r in asg.iterrows():
        dur = dur_turno(r["hora_inicio"], r["hora_fin"]); idx = contador[r["hora_inicio"]]; contador[r["hora_inicio"]] += 1
        gen = breaks_colombia if r["pais"] == "Colombia" else breaks_espana
        for bhi, dm, tp in gen(r["hora_inicio"], round(dur), idx):
            filas_b.append({"asignacion_id": int(r["asignacion_id"]),
                            "hora_inicio": str(bhi), "duracion_min": int(dm), "tipo": str(tp)})

    ids = [int(x) for x in asg["asignacion_id"].tolist()]
    try:
        # DELETE de los breaks viejos: un solo DELETE por todos los ids (con índice es rápido)
        with engine.begin() as conn:
            if ids:
                conn.execute(text("DELETE FROM breaks WHERE asignacion_id = ANY(:ids)"), {"ids": ids})

        # INSERT MULTI-FILA: muchas filas por sentencia (VALUES (...),(...),...),
        # en vez de una fila por viaje. Reduce miles de viajes a la base a unas pocas decenas.
        LOTE = 500
        with engine.begin() as conn:
            for k in range(0, len(filas_b), LOTE):
                lote = filas_b[k:k+LOTE]
                valores = []
                params = {}
                for j, f in enumerate(lote):
                    valores.append(f"(:a{j}, :h{j}, :d{j}, :t{j})")
                    params[f"a{j}"] = f["asignacion_id"]
                    params[f"h{j}"] = f["hora_inicio"]
                    params[f"d{j}"] = f["duracion_min"]
                    params[f"t{j}"] = f["tipo"]
                sql = "INSERT INTO breaks (asignacion_id, hora_inicio, duracion_min, tipo) VALUES " + ", ".join(valores)
                conn.execute(text(sql), params)
    except Exception as e:
        ejemplo = filas_b[0] if filas_b else {}
        raise HTTPException(500, f"Error al escribir breaks: {type(e).__name__}: {str(e)[:300]} | ejemplo: {ejemplo}")

    return {"ok": True, "mes": mes, "breaks": len(filas_b)}


# ============================================================
#  ADHERENCIA · Fase 2 — Importar resumen diario del ACD
# ============================================================
# Mapa: clave logica -> posibles inicios del nombre de columna en el Excel.
# Se busca por "empieza por" para tolerar nombres recortados (ej "Total Not Ready Ti").
_ACD_COLS = {
    "fecha":            ["Fecha"],
    "plataforma":       ["Plataforma"],
    "login_acd":        ["Agente"],
    "logado":           ["Logado"],
    "deslogado":        ["Deslogado"],
    "seg_login":        ["Total Login"],
    "seg_not_ready":    ["Total Not Ready"],
    # Formato nuevo del ACD: Status 1..8 (2=PAUSA,3=DESCANSO,4=FORMACION,5=BO,6=SERVICIO).
    # Si no estan, cae a los nombres del formato viejo.
    "seg_pausa_visual": ["Total Status 2", "Pausa Visual"],
    "seg_break":        ["Total Status 3", "Break"],
    "seg_formacion":    ["Total Status 4", "Formacion", "Formaci\u00f3n"],
    "seg_backoffice":   ["Total Status 5", "BackOffice", "Back Office"],   # BO = productivo
    "seg_servicios":    ["Total Status 6", "Servicios"],
    "seg_talk_in":      ["Total Talk Time In"],
    "seg_talk_out":     ["Total Talk Time Ou"],
    "seg_hold":         ["Total Hold"],
    "llamadas_inbound": ["Total Calls Inboun"],
}


def _buscar_col(columnas, prefijos):
    """Nombre real de la columna que empieza por alguno de los prefijos (ignora may/espacios)."""
    norm = {c: str(c).strip().lower() for c in columnas}
    for p in prefijos:
        pl = p.strip().lower()
        for real, n in norm.items():
            if n.startswith(pl):
                return real
    return None


def _a_int(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return 0
        return int(round(float(v)))
    except Exception:
        return 0


def _a_txt(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s if s and s.lower() != "nan" else None


def _a_hora(v):
    """'10:15' o time de Excel -> 'HH:MM:SS' (texto). Vacio -> None."""
    s = _a_txt(v)
    if not s:
        return None
    partes = s.split(":")
    try:
        h = int(partes[0]); m = int(partes[1]) if len(partes) > 1 else 0
        return f"{h:02d}:{m:02d}:00"
    except Exception:
        return None


@app.post("/acd/importar")
async def acd_importar(
    x_api_key: str = Header(None),
    archivo: UploadFile = File(...),
):
    """
    Sube el Excel del ACD (resumen por agente/dia) -> acd_resumen_diario.
    Detecta la cabecera sola (formato nuevo fila 1 con Status 1..8, o viejo fila 3).
    Tiempos en segundos. Upsert por (login_acd, fecha). Cruza login_acd con agentes.
    """
    check_key(x_api_key)
    engine = get_engine()

    file_bytes = await archivo.read()
    # Detectar la fila de cabecera: formato nuevo -> fila 1 (indice 0); viejo -> fila 3 (indice 2).
    df = None
    for h in (0, 2):
        tmp = pd.read_excel(io.BytesIO(file_bytes), header=h, dtype=str)
        cols = [str(c).strip().lower() for c in tmp.columns]
        if "fecha" in cols and "agente" in cols:
            df = tmp
            break
    if df is None:
        raise HTTPException(400, "No encuentro la cabecera (columnas 'Fecha'/'Agente') en el Excel.")
    df.columns = [str(c).strip() for c in df.columns]

    mapa = {clave: _buscar_col(df.columns, prefijos) for clave, prefijos in _ACD_COLS.items()}
    faltan = [k for k in ("fecha", "login_acd") if mapa[k] is None]
    if faltan:
        raise HTTPException(400, f"Faltan columnas obligatorias {faltan}. Columnas vistas: {list(df.columns)}")

    filas = []
    for _, r in df.iterrows():
        fecha_s = _a_txt(r.get(mapa["fecha"]))
        login_s = _a_txt(r.get(mapa["login_acd"]))
        if not fecha_s or not login_s:
            continue
        try:
            fecha = pd.to_datetime(fecha_s, format="%Y%m%d").date()
        except Exception:
            continue
        filas.append({
            "fecha": fecha,
            "plataforma": _a_txt(r.get(mapa["plataforma"])) if mapa["plataforma"] else None,
            "login_acd": login_s,
            "logado": _a_hora(r.get(mapa["logado"])) if mapa["logado"] else None,
            "deslogado": _a_hora(r.get(mapa["deslogado"])) if mapa["deslogado"] else None,
            "seg_login": _a_int(r.get(mapa["seg_login"])) if mapa["seg_login"] else 0,
            "seg_not_ready": _a_int(r.get(mapa["seg_not_ready"])) if mapa["seg_not_ready"] else 0,
            "seg_pausa_visual": _a_int(r.get(mapa["seg_pausa_visual"])) if mapa["seg_pausa_visual"] else 0,
            "seg_break": _a_int(r.get(mapa["seg_break"])) if mapa["seg_break"] else 0,
            "seg_formacion": _a_int(r.get(mapa["seg_formacion"])) if mapa["seg_formacion"] else 0,
            "seg_servicios": _a_int(r.get(mapa["seg_servicios"])) if mapa["seg_servicios"] else 0,
            "seg_backoffice": _a_int(r.get(mapa["seg_backoffice"])) if mapa["seg_backoffice"] else 0,
            "seg_talk_in": _a_int(r.get(mapa["seg_talk_in"])) if mapa["seg_talk_in"] else 0,
            "seg_talk_out": _a_int(r.get(mapa["seg_talk_out"])) if mapa["seg_talk_out"] else 0,
            "seg_hold": _a_int(r.get(mapa["seg_hold"])) if mapa["seg_hold"] else 0,
            "llamadas_inbound": _a_int(r.get(mapa["llamadas_inbound"])) if mapa["llamadas_inbound"] else 0,
        })

    if not filas:
        raise HTTPException(400, "El Excel no tenia filas validas (revisa que la cabecera este en la fila 3).")

    cols = ["fecha", "plataforma", "login_acd", "logado", "deslogado", "seg_login",
            "seg_not_ready", "seg_pausa_visual", "seg_break", "seg_formacion", "seg_servicios",
            "seg_backoffice", "seg_talk_in", "seg_talk_out", "seg_hold", "llamadas_inbound"]
    actualiza = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in ("login_acd", "fecha"))
    conflict = f" ON CONFLICT (login_acd, fecha) DO UPDATE SET {actualiza} "

    LOTE = 500
    try:
        with engine.begin() as conn:
            for k in range(0, len(filas), LOTE):
                lote = filas[k:k + LOTE]
                valores = []; params = {}
                for j, f in enumerate(lote):
                    valores.append("(" + ", ".join(f":{c}{j}" for c in cols) + ")")
                    for c in cols:
                        params[f"{c}{j}"] = f[c]
                sql = "INSERT INTO acd_resumen_diario (" + ", ".join(cols) + ") VALUES " + ", ".join(valores) + conflict
                conn.execute(text(sql), params)
            # Cruce login_acd -> agente_id (ambos a texto para evitar choques de tipo)
            conn.execute(text("""
                UPDATE acd_resumen_diario d
                SET agente_id = a.id
                FROM agentes a
                WHERE a.login_acd::text = d.login_acd
            """))
    except Exception as e:
        ejemplo = filas[0] if filas else {}
        raise HTTPException(500, f"Error al cargar ACD: {type(e).__name__}: {str(e)[:300]} | ejemplo: {ejemplo}")

    rep = pd.read_sql(text("""
        SELECT COUNT(*) AS filas, MIN(fecha) AS desde, MAX(fecha) AS hasta,
               COUNT(DISTINCT login_acd) AS logins,
               COUNT(DISTINCT login_acd) FILTER (WHERE agente_id IS NOT NULL) AS cruzados,
               COUNT(DISTINCT login_acd) FILTER (WHERE agente_id IS NULL) AS sin_cruzar
        FROM acd_resumen_diario
    """), engine)
    sin = pd.read_sql(text("SELECT DISTINCT login_acd FROM acd_resumen_diario WHERE agente_id IS NULL ORDER BY login_acd LIMIT 30"), engine)

    return {
        "ok": True,
        "filas_excel_validas": len(filas),
        "total_filas_tabla": int(rep["filas"][0]),
        "rango": {"desde": str(rep["desde"][0]), "hasta": str(rep["hasta"][0])},
        "logins_distintos": int(rep["logins"][0]),
        "logins_cruzados": int(rep["cruzados"][0]),
        "logins_sin_cruzar": int(rep["sin_cruzar"][0]),
        "ejemplos_sin_cruzar": sin["login_acd"].tolist(),
    }


# ============================================================
#  ADHERENCIA · Fase 3 — Calcular adherencia (plan vs ACD)
# ============================================================
@app.post("/adherencia/calcular")
def adherencia_calcular(
    x_api_key: str = Header(None),
    desde: str = None,   # 'YYYY-MM-DD' opcional; si vacio usa el minimo del ACD
    hasta: str = None,   # 'YYYY-MM-DD' opcional; si vacio usa el maximo del ACD
):
    """
    Cruza el plan (asignaciones, tipo='trabajo') con la realidad (acd_resumen_diario)
    por (agente_id, fecha) y calcula: ADH BRUTA, ADH NETA, UTILIZACION, PRODUCTIVIDAD,
    INFOE, mas TMO y llamadas. HR PRESENCIA sale del turno. Upsert en adherencia.
    """
    check_key(x_api_key)
    engine = get_engine()

    rango = pd.read_sql(text("SELECT MIN(fecha) AS d, MAX(fecha) AS h FROM acd_resumen_diario"), engine)
    if rango["d"][0] is None:
        raise HTTPException(400, "No hay datos en acd_resumen_diario. Importa el Excel primero.")
    d_ini = desde or str(rango["d"][0])
    d_fin = hasta or str(rango["h"][0])

    df = pd.read_sql(text("""
        SELECT a.agente_id, a.fecha, a.hora_inicio, a.hora_fin,
               d.plataforma, d.seg_login, d.seg_not_ready,
               d.seg_talk_in, d.seg_talk_out, d.seg_hold,
               d.seg_backoffice, d.llamadas_inbound
        FROM asignaciones a
        JOIN acd_resumen_diario d
          ON d.agente_id = a.agente_id AND d.fecha = a.fecha
        WHERE a.tipo = 'trabajo'
          AND a.hora_inicio IS NOT NULL AND a.hora_fin IS NOT NULL
          AND a.fecha BETWEEN :i AND :f
    """), engine, params={"i": d_ini, "f": d_fin})

    gap = pd.read_sql(text("""
        SELECT COUNT(*) AS n, COUNT(DISTINCT a.agente_id) AS agentes
        FROM asignaciones a
        LEFT JOIN acd_resumen_diario d
          ON d.agente_id = a.agente_id AND d.fecha = a.fecha
        WHERE a.tipo = 'trabajo'
          AND a.hora_inicio IS NOT NULL AND a.hora_fin IS NOT NULL
          AND a.fecha BETWEEN :i AND :f
          AND d.id IS NULL
    """), engine, params={"i": d_ini, "f": d_fin})

    if df.empty:
        return {"ok": True, "vacio": True, "rango": {"desde": d_ini, "hasta": d_fin},
                "nota": "No hay dias con plan 'trabajo' y dato ACD a la vez en ese rango.",
                "plan_sin_acd_filas": int(gap["n"][0]), "plan_sin_acd_agentes": int(gap["agentes"][0])}

    def pct(num, den):
        if not den or den <= 0:
            return None
        v = round(100.0 * num / den, 2)
        return min(v, 999.99)   # acota ruido por denominador pequeno y evita overflow numeric(6,2)

    filas = []
    for _, r in df.iterrows():
        seg_presencia = int(round(dur_turno(r["hora_inicio"], r["hora_fin"]) * 3600))
        seg_login = int(r["seg_login"] or 0)
        seg_nr = int(r["seg_not_ready"] or 0)
        seg_talk = int(r["seg_talk_in"] or 0) + int(r["seg_talk_out"] or 0)
        seg_hold = int(r["seg_hold"] or 0)
        seg_bo = int(r["seg_backoffice"] or 0)            # BO = unico estado productivo
        seg_efectiva = max(seg_login - seg_nr, 0)
        seg_productiva = seg_talk + seg_bo
        llam = int(r["llamadas_inbound"] or 0)
        filas.append({
            "agente_id": int(r["agente_id"]),
            "fecha": pd.Timestamp(r["fecha"]).date(),
            "plataforma": (str(r["plataforma"]) if r["plataforma"] is not None else None),
            "seg_presencia": seg_presencia,
            "seg_login": seg_login,
            "seg_efectiva": seg_efectiva,
            "seg_productiva": seg_productiva,
            "seg_talk": seg_talk,
            "seg_hold": seg_hold,
            "seg_not_ready": seg_nr,
            "adh_bruta": pct(seg_login, seg_presencia),         # HR Logado / HR Presencia
            "adh_neta": pct(seg_efectiva, seg_presencia),       # HR Efectiva / HR Presencia
            "utilizacion": pct(seg_efectiva, seg_login),        # HR Efectiva / HR Logado
            "productividad": pct(seg_productiva, seg_efectiva), # HR Productiva / HR Efectiva
            "infoe": pct(seg_productiva, seg_login),            # HR Productiva / HR Logado
            "llamadas": llam,
            "tmo_seg": (round((seg_talk + seg_hold) / llam, 2) if llam > 0 else None),
        })

    cols = ["agente_id", "fecha", "plataforma", "seg_presencia", "seg_login", "seg_efectiva",
            "seg_productiva", "seg_talk", "seg_hold", "seg_not_ready", "adh_bruta", "adh_neta",
            "utilizacion", "productividad", "infoe", "llamadas", "tmo_seg"]
    actualiza = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in ("agente_id", "fecha"))
    conflict = f" ON CONFLICT (agente_id, fecha) DO UPDATE SET {actualiza} "

    LOTE = 500
    try:
        with engine.begin() as conn:
            for k in range(0, len(filas), LOTE):
                lote = filas[k:k + LOTE]
                valores = []; params = {}
                for j, f in enumerate(lote):
                    valores.append("(" + ", ".join(f":{c}{j}" for c in cols) + ")")
                    for c in cols:
                        params[f"{c}{j}"] = f[c]
                sql = "INSERT INTO adherencia (" + ", ".join(cols) + ") VALUES " + ", ".join(valores) + conflict
                conn.execute(text(sql), params)
    except Exception as e:
        ejemplo = filas[0] if filas else {}
        raise HTTPException(500, f"Error al escribir adherencia: {type(e).__name__}: {str(e)[:300]} | ejemplo: {ejemplo}")

    resumen = pd.read_sql(text("""
        SELECT COUNT(*) AS filas, COUNT(DISTINCT agente_id) AS agentes,
               ROUND(100.0*SUM(seg_login)   /NULLIF(SUM(seg_presencia),0),1) AS adh_bruta,
               ROUND(100.0*SUM(seg_efectiva)/NULLIF(SUM(seg_presencia),0),1) AS adh_neta,
               ROUND(100.0*SUM(seg_efectiva)/NULLIF(SUM(seg_login),0),1)     AS utilizacion,
               ROUND(100.0*SUM(seg_productiva)/NULLIF(SUM(seg_efectiva),0),1) AS productividad,
               ROUND(100.0*SUM(seg_productiva)/NULLIF(SUM(seg_login),0),1)   AS infoe,
               ROUND(SUM(seg_talk+seg_hold)::numeric/NULLIF(SUM(llamadas),0),1) AS tmo,
               SUM(llamadas) AS llamadas
        FROM adherencia WHERE fecha BETWEEN :i AND :f
    """), engine, params={"i": d_ini, "f": d_fin})
    rr = resumen.iloc[0]
    def fv(x):
        return None if pd.isna(x) else float(x)

    return {
        "ok": True,
        "rango": {"desde": d_ini, "hasta": d_fin},
        "filas_calculadas": len(filas),
        "agentes_cubiertos": int(rr["agentes"]),
        "adh_bruta_pct": fv(rr["adh_bruta"]),
        "adh_neta_pct": fv(rr["adh_neta"]),
        "utilizacion_pct": fv(rr["utilizacion"]),
        "productividad_pct": fv(rr["productividad"]),
        "infoe_pct": fv(rr["infoe"]),
        "tmo_seg": fv(rr["tmo"]),
        "llamadas_total": int(rr["llamadas"] or 0),
        "diagnostico_plan_sin_acd": {"filas": int(gap["n"][0]), "agentes": int(gap["agentes"][0])},
    }


# ============================================================
#  ADHERENCIA · Fase 3b — Importar parrilla historica -> asignaciones
# ============================================================
import re as _re_par

_PAR_LIBRE = {"LIBRE", "DLF", "FEST"}
_PAR_VAC = {"VAC", "VACACIONES"}
_PAR_AUS_PREFIJOS = ("BMED", "BMLD", "BAJA", "APNJ", "ANJ", "SUSP", "PER", "PERM",
                     "PR", "SNT", "SINT")


def _par_segmento(seg):
    """'09:00-18:00' -> (inicio_min, duracion_min). Tolera '.' como ':'. None si no parsea."""
    seg = seg.strip().replace(".", ":")
    m = _re_par.match(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$", seg)
    if not m:
        return None
    h1, m1, h2, m2 = map(int, m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        return None
    ini = h1 * 60 + m1
    fin = h2 * 60 + m2
    dur = fin - ini
    if dur <= 0:
        dur += 24 * 60
    return ini, dur


def _par_celda(valor):
    """
    Mapea una celda de la parrilla a (tipo, hora_inicio, hora_fin).
    - turno (incl. partido con '/') -> ('trabajo', 'HH:MM', 'HH:MM')  [ventana = duracion total]
    - LIBRE/FEST/DLF -> ('libre', None, None)
    - VAC -> ('vacaciones', None, None)
    - BMED/BAJA/APNJ/SUSP/PERM... -> ('ausencia', None, None)
    - vacio -> None (no se inserta) ; no parseable -> 'SKIP'
    """
    if valor is None:
        return None
    s = str(valor).strip()
    if not s:
        return None
    up = s.upper()
    if up in _PAR_LIBRE:
        return ("libre", None, None)
    if up in _PAR_VAC:
        return ("vacaciones", None, None)
    if any(up.startswith(p) for p in _PAR_AUS_PREFIJOS):
        return ("ausencia", None, None)
    # turno (uno o varios segmentos separados por '/')
    primer_ini = None
    total_dur = 0
    for seg in s.split("/"):
        r = _par_segmento(seg)
        if r is None:
            return "SKIP"
        ini, dur = r
        if primer_ini is None:
            primer_ini = ini
        total_dur += dur
    if primer_ini is None or total_dur <= 0:
        return "SKIP"
    hi = f"{(primer_ini // 60) % 24:02d}:{primer_ini % 60:02d}"
    fin_min = (primer_ini + total_dur) % (24 * 60)
    hf = f"{fin_min // 60:02d}:{fin_min % 60:02d}"
    return ("trabajo", hi, hf)


def _mes_de_hoja(nombre):
    n = nombre.strip().lower()
    if n.startswith("abr"):
        return 4
    if n.startswith("may"):
        return 5
    if n.startswith("jun") or n.startswith("jum"):
        return 6
    return None


@app.post("/asignaciones/importar-parrilla")
async def importar_parrilla(
    x_api_key: str = Header(None),
    archivo: UploadFile = File(...),
    meses: str = "4,5",            # meses a cargar (numero), separados por coma. Junio NO por defecto.
    campana: str = "Endesa",
):
    """
    Carga una parrilla matricial (1 fila por agente, 1 columna por dia) en asignaciones.
    Cruza por DNI con la tabla agentes. Por defecto solo abril y mayo (junio ya esta cargado).
    """
    check_key(x_api_key)
    engine = get_engine()

    meses_ok = {int(x) for x in str(meses).split(",") if x.strip().isdigit()}
    if not meses_ok:
        raise HTTPException(400, "Parametro 'meses' invalido (ej: '4,5').")

    id_campana = pd.read_sql(text("SELECT id FROM campanas WHERE nombre=:n"),
                             engine, params={"n": campana})
    if id_campana.empty:
        raise HTTPException(400, f"No existe la campana {campana!r}.")
    id_campana = int(id_campana["id"][0])

    # mapa DNI -> agente_id
    ag = pd.read_sql(text("SELECT id, dni FROM agentes"), engine)
    dni2id = {str(d).strip(): int(i) for i, d in zip(ag["id"], ag["dni"]) if d is not None}

    file_bytes = await archivo.read()
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)

    ID_COLS = 6          # ESTADO, Transporte, DNI, APELLIDO1, APELLIDO2, NOMBRE
    COL_DNI = 2

    filas = []
    dnis_sin_cruzar = set()
    celdas_skip = 0
    por_tipo = {"trabajo": 0, "libre": 0, "vacaciones": 0, "ausencia": 0}

    for sn in wb.sheetnames:
        mes = _mes_de_hoja(sn)
        if mes is None or mes not in meses_ok:
            continue
        ws = wb[sn]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        header = rows[0]
        # columnas de fecha de ESTE mes (cabecera tipo datetime y month == mes)
        cols_fecha = []
        for ci in range(ID_COLS, len(header)):
            h = header[ci]
            if isinstance(h, datetime) and h.month == mes:
                cols_fecha.append((ci, h.date()))
        for r in rows[1:]:
            if len(r) <= COL_DNI or r[COL_DNI] is None:
                continue
            dni = str(r[COL_DNI]).strip()
            aid = dni2id.get(dni)
            if aid is None:
                dnis_sin_cruzar.add(dni)
                continue
            for ci, fecha in cols_fecha:
                if ci >= len(r):
                    continue
                res = _par_celda(r[ci])
                if res is None:
                    continue
                if res == "SKIP":
                    celdas_skip += 1
                    continue
                tipo, hi, hf = res
                por_tipo[tipo] = por_tipo.get(tipo, 0) + 1
                filas.append({
                    "agente_id": aid, "fecha": fecha, "campana_id": id_campana,
                    "turno_id": None, "hora_inicio": hi, "hora_fin": hf,
                    "tipo": tipo, "creado_por": "import-parrilla", "bloqueado": True,
                })

    if not filas:
        raise HTTPException(400, f"No se generaron filas. Meses pedidos: {sorted(meses_ok)}. "
                                 f"DNIs sin cruzar: {len(dnis_sin_cruzar)}.")

    cols = ["agente_id", "fecha", "campana_id", "turno_id", "hora_inicio", "hora_fin",
            "tipo", "creado_por", "bloqueado"]
    actualiza = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in ("agente_id", "fecha"))
    conflict = f" ON CONFLICT (agente_id, fecha) DO UPDATE SET {actualiza} "

    LOTE = 500
    try:
        with engine.begin() as conn:
            for k in range(0, len(filas), LOTE):
                lote = filas[k:k + LOTE]
                valores = []; params = {}
                for j, f in enumerate(lote):
                    valores.append("(" + ", ".join(f":{c}{j}" for c in cols) + ")")
                    for c in cols:
                        params[f"{c}{j}"] = f[c]
                sql = "INSERT INTO asignaciones (" + ", ".join(cols) + ") VALUES " + ", ".join(valores) + conflict
                conn.execute(text(sql), params)
    except Exception as e:
        ejemplo = filas[0] if filas else {}
        raise HTTPException(500, f"Error al escribir asignaciones: {type(e).__name__}: {str(e)[:300]} | ejemplo: {ejemplo}")

    return {
        "ok": True,
        "meses_cargados": sorted(meses_ok),
        "filas_insertadas": len(filas),
        "por_tipo": por_tipo,
        "agentes_cruzados": len({f["agente_id"] for f in filas}),
        "dnis_sin_cruzar": len(dnis_sin_cruzar),
        "ejemplos_dnis_sin_cruzar": sorted(dnis_sin_cruzar)[:20],
        "celdas_no_parseables": celdas_skip,
    }


# ============================================================
#  ADHERENCIA · Fase 4 — Datos para el dashboard
# ============================================================
@app.get("/adherencia/opciones")
def adherencia_opciones(x_api_key: str = Header(None)):
    """Valores para los filtros del dashboard (rango de fechas, paises, centros)."""
    check_key(x_api_key)
    engine = get_engine()
    r = pd.read_sql(text("SELECT MIN(fecha) AS d, MAX(fecha) AS h FROM adherencia"), engine)
    if r["d"][0] is None:
        return {"ok": True, "vacio": True}
    pc = pd.read_sql(text("""
        SELECT DISTINCT ag.pais, ag.centro
        FROM adherencia adh JOIN agentes ag ON ag.id = adh.agente_id
    """), engine)
    return {
        "ok": True, "vacio": False,
        "desde": str(r["d"][0]), "hasta": str(r["h"][0]),
        "paises": sorted([str(p) for p in pc["pais"].dropna().unique()]),
        "centros": sorted([str(c) for c in pc["centro"].dropna().unique()]),
    }


@app.get("/adherencia/dashboard")
def adherencia_dashboard(
    x_api_key: str = Header(None),
    desde: str = None,
    hasta: str = None,
    pais: str = None,
    centro: str = None,
):
    """
    Datos agregados de adherencia para el dashboard, segun filtros.
    Adherencia y TMO se calculan ponderados por segundos/llamadas (no promedio simple).
    """
    check_key(x_api_key)
    engine = get_engine()

    rango = pd.read_sql(text("SELECT MIN(fecha) AS d, MAX(fecha) AS h FROM adherencia"), engine)
    if rango["d"][0] is None:
        return {"ok": True, "vacio": True}
    d_ini = desde or str(rango["d"][0])
    d_fin = hasta or str(rango["h"][0])

    cond = "adh.fecha BETWEEN :i AND :f"
    params = {"i": d_ini, "f": d_fin}
    if pais:
        cond += " AND ag.pais = :pais"; params["pais"] = pais
    if centro:
        cond += " AND ag.centro = :centro"; params["centro"] = centro

    df = pd.read_sql(text(f"""
        SELECT adh.agente_id, adh.fecha, adh.seg_programados, adh.seg_adherido,
               adh.seg_talk, adh.llamadas, ag.nombre, ag.centro, ag.pais
        FROM adherencia adh
        JOIN agentes ag ON ag.id = adh.agente_id
        WHERE {cond}
    """), engine, params=params)

    if df.empty:
        return {"ok": True, "vacio": False, "sin_datos_filtro": True,
                "rango": {"desde": d_ini, "hasta": d_fin}}

    def _adh(g):
        sp = g["seg_programados"].sum()
        return round(100.0 * g["seg_adherido"].sum() / sp, 1) if sp > 0 else None

    def _tmo(g):
        ll = g["llamadas"].sum()
        return round(g["seg_talk"].sum() / ll, 1) if ll > 0 else None

    kpis = {
        "adherencia_pct": _adh(df),
        "tmo_seg": _tmo(df),
        "llamadas": int(df["llamadas"].sum()),
        "agentes": int(df["agente_id"].nunique()),
        "dias": int(df["fecha"].nunique()),
        "filas": int(len(df)),
    }

    por_fecha = []
    for fch, g in df.groupby("fecha"):
        por_fecha.append({"fecha": str(fch), "adherencia_pct": _adh(g),
                          "llamadas": int(g["llamadas"].sum()), "tmo_seg": _tmo(g)})
    por_fecha.sort(key=lambda x: x["fecha"])

    por_agente = []
    for aid, g in df.groupby("agente_id"):
        por_agente.append({
            "agente_id": int(aid), "nombre": str(g["nombre"].iloc[0]),
            "centro": (None if pd.isna(g["centro"].iloc[0]) else str(g["centro"].iloc[0])),
            "pais": (None if pd.isna(g["pais"].iloc[0]) else str(g["pais"].iloc[0])),
            "adherencia_pct": _adh(g), "tmo_seg": _tmo(g),
            "llamadas": int(g["llamadas"].sum()), "dias": int(g["fecha"].nunique()),
        })
    por_agente.sort(key=lambda x: (x["adherencia_pct"] is None, x["adherencia_pct"]))

    por_pais = []
    for ps, g in df.groupby("pais"):
        por_pais.append({"pais": str(ps), "adherencia_pct": _adh(g), "tmo_seg": _tmo(g),
                         "llamadas": int(g["llamadas"].sum()),
                         "agentes": int(g["agente_id"].nunique())})

    return {
        "ok": True, "vacio": False,
        "rango": {"desde": d_ini, "hasta": d_fin},
        "kpis": kpis,
        "por_fecha": por_fecha,
        "por_agente": por_agente,
        "por_pais": por_pais,
    }


# ============================================================
#  CONSOLA · Fase 3 — Agregar eventos de la consola -> acd_resumen_diario
# ============================================================
def _hora_local(ts, pais):
    """timestamptz -> 'HH:MM:SS' en la zona del agente. None si no hay valor."""
    if ts is None or pd.isna(ts):
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    tz = "Europe/Madrid" if str(pais) == "España" else "America/Bogota"
    try:
        return t.tz_convert(tz).strftime("%H:%M:%S")
    except Exception:
        return t.strftime("%H:%M:%S")


@app.post("/acd/agregar-consola")
def agregar_consola(
    x_api_key: str = Header(None),
    desde: str = None,   # 'YYYY-MM-DD' opcional; por defecto hoy
    hasta: str = None,
):
    """
    Resume los eventos de la consola por agente/dia y los escribe en acd_resumen_diario
    (origen='consola'), para que adherencia los calcule igual que el ACD del Excel.
    Eventos abiertos (fin NULL) se cuentan hasta ahora. Upsert por (login_acd, fecha).
    """
    check_key(x_api_key)
    engine = get_engine()

    hoy = pd.read_sql(text("SELECT (now() at time zone 'America/Bogota')::date AS d"), engine)["d"][0]
    d_ini = desde or str(hoy)
    d_fin = hasta or str(hoy)

    ev = pd.read_sql(text("""
        SELECT e.agente_id, e.fecha, e.estado,
               extract(epoch from (coalesce(e.fin, now()) - e.inicio)) AS seg,
               e.inicio, e.fin
        FROM eventos_acd e
        WHERE e.fecha BETWEEN :i AND :f
    """), engine, params={"i": d_ini, "f": d_fin})

    if ev.empty:
        return {"ok": True, "vacio": True, "rango": {"desde": d_ini, "hasta": d_fin},
                "nota": "No hay eventos de consola en ese rango."}

    ev["seg"] = ev["seg"].fillna(0).clip(lower=0).astype(int)

    ag = pd.read_sql(text("SELECT id, login_acd, pais FROM agentes"), engine)
    login_de = {int(i): (str(l) if l is not None else None) for i, l in zip(ag["id"], ag["login_acd"])}
    pais_de = {int(i): p for i, p in zip(ag["id"], ag["pais"])}

    NO_READY = {"break", "almuerzo", "descanso", "pausa_visual", "formacion",
                "no_disponible", "bano", "reunion", "gestion_bo"}

    filas = []
    sin_login = set()
    for (aid, fecha), g in ev.groupby(["agente_id", "fecha"]):
        aid = int(aid)
        login = login_de.get(aid)
        if not login:
            sin_login.add(aid)
            continue
        def s(estado):
            return int(g.loc[g["estado"] == estado, "seg"].sum())
        filas.append({
            "fecha": str(fecha),
            "plataforma": "CONSOLA",
            "login_acd": login,
            "agente_id": aid,
            "logado": _hora_local(g["inicio"].min(), pais_de.get(aid)),
            "deslogado": _hora_local(g["fin"].max(), pais_de.get(aid)),
            "seg_login": int(g["seg"].sum()),
            "seg_not_ready": int(g.loc[g["estado"].isin(NO_READY), "seg"].sum()),
            "seg_pausa_visual": s("pausa_visual"),
            "seg_break": s("break"),
            "seg_formacion": s("formacion"),
            "seg_servicios": 0,
            "seg_backoffice": s("gestion_bo"),
            "seg_talk_in": 0,     # sin telefonia aun
            "seg_talk_out": 0,
            "seg_hold": 0,
            "llamadas_inbound": 0,
        })

    if not filas:
        return {"ok": True, "vacio": False, "rango": {"desde": d_ini, "hasta": d_fin},
                "filas": 0, "agentes_sin_login_acd": sorted(sin_login),
                "nota": "Hay eventos pero ningun agente con login_acd."}

    cols = ["fecha", "plataforma", "login_acd", "agente_id", "logado", "deslogado", "seg_login",
            "seg_not_ready", "seg_pausa_visual", "seg_break", "seg_formacion", "seg_servicios",
            "seg_backoffice", "seg_talk_in", "seg_talk_out", "seg_hold", "llamadas_inbound"]
    actualiza = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in ("login_acd", "fecha"))
    conflict = f" ON CONFLICT (login_acd, fecha) DO UPDATE SET {actualiza} "

    LOTE = 500
    try:
        with engine.begin() as conn:
            for k in range(0, len(filas), LOTE):
                lote = filas[k:k + LOTE]
                valores = []; params = {}
                for j, f in enumerate(lote):
                    valores.append("(" + ", ".join(f":{c}{j}" for c in cols) + ")")
                    for c in cols:
                        params[f"{c}{j}"] = f[c]
                sql = "INSERT INTO acd_resumen_diario (" + ", ".join(cols) + ") VALUES " + ", ".join(valores) + conflict
                conn.execute(text(sql), params)
            conn.execute(text("""
                UPDATE acd_resumen_diario d SET agente_id = a.id
                FROM agentes a WHERE a.login_acd::text = d.login_acd AND d.agente_id IS NULL
            """))
    except Exception as e:
        raise HTTPException(500, f"Error al agregar consola: {type(e).__name__}: {str(e)[:300]} | ejemplo: {filas[0]}")

    return {
        "ok": True,
        "rango": {"desde": d_ini, "hasta": d_fin},
        "filas_escritas": len(filas),
        "agentes": len({f["agente_id"] for f in filas}),
        "agentes_sin_login_acd": sorted(sin_login),
        "nota": "Para el dia en curso es acumulado hasta ahora; al cerrar la jornada queda completo. Ahora corre /adherencia/calcular.",
    }


# ============================================================
#  ADHERENCIA · Export a Excel (para gerencia)
# ============================================================
@app.get("/adherencia/export")
def adherencia_export(
    x_api_key: str = Header(None),
    desde: str = None,
    hasta: str = None,
    pais: str = None,
    centro: str = None,
    modo: str = None,
    supervisor: str = None,
):
    """Descarga un .xlsx con la adherencia (detalle + resumenes) segun filtros."""
    check_key(x_api_key)
    engine = get_engine()

    rango = pd.read_sql(text("SELECT MIN(fecha) AS d, MAX(fecha) AS h FROM adherencia"), engine)
    if rango["d"][0] is None:
        raise HTTPException(400, "No hay datos de adherencia. Corre /adherencia/calcular primero.")
    d_ini = desde or str(rango["d"][0])
    d_fin = hasta or str(rango["h"][0])

    cond = "adh.fecha BETWEEN :i AND :f"
    params = {"i": d_ini, "f": d_fin}
    if pais:       cond += " AND ag.pais = :pais";              params["pais"] = pais
    if centro:     cond += " AND ag.centro = :centro";          params["centro"] = centro
    if modo:       cond += " AND ag.modo = :modo";              params["modo"] = modo
    if supervisor: cond += " AND ag.supervisor_nombre = :sup";  params["sup"] = supervisor

    df = pd.read_sql(text(f"""
        SELECT adh.fecha, ag.nombre, ag.dni, ag.centro, ag.pais, ag.modo, ag.supervisor_nombre,
               adh.seg_presencia, adh.seg_login, adh.seg_efectiva, adh.seg_productiva,
               adh.seg_talk, adh.seg_hold, adh.llamadas, adh.tmo_seg,
               adh.adh_bruta, adh.adh_neta, adh.utilizacion, adh.productividad, adh.infoe
        FROM adherencia adh JOIN agentes ag ON ag.id = adh.agente_id
        WHERE {cond}
        ORDER BY ag.nombre, adh.fecha
    """), engine, params=params)
    if df.empty:
        raise HTTPException(404, "No hay datos para esos filtros.")

    def kpis(g):
        sp, sl, se = g["seg_presencia"].sum(), g["seg_login"].sum(), g["seg_efectiva"].sum()
        spr, ll = g["seg_productiva"].sum(), g["llamadas"].sum()
        f = lambda n, d: (round(100.0 * n / d, 1) if d > 0 else None)
        return pd.Series({
            "HR Presencia": round(sp / 3600, 1), "HR Logado": round(sl / 3600, 1),
            "HR Efectiva": round(se / 3600, 1), "HR Productiva": round(spr / 3600, 1),
            "ADH Bruta %": f(sl, sp), "ADH Neta %": f(se, sp), "Utilizacion %": f(se, sl),
            "Productividad %": f(spr, se), "INFOE %": f(spr, sl),
            "Llamadas": int(ll),
            "TMO (mm:ss)": (f"{int((g['seg_talk'].sum()+g['seg_hold'].sum())//ll)//60:02d}:"
                            f"{int((g['seg_talk'].sum()+g['seg_hold'].sum())//ll)%60:02d}" if ll > 0 else ""),
            "Dias": g["fecha"].nunique(),
        })

    # Detalle por agente/dia
    det = df.copy()
    det["HR Presencia"] = (det["seg_presencia"] / 3600).round(2)
    det["HR Logado"] = (det["seg_login"] / 3600).round(2)
    det["HR Efectiva"] = (det["seg_efectiva"] / 3600).round(2)
    det["HR Productiva"] = (det["seg_productiva"] / 3600).round(2)
    detalle = det[["fecha", "nombre", "dni", "centro", "pais", "modo", "supervisor_nombre",
                   "HR Presencia", "HR Logado", "HR Efectiva", "HR Productiva",
                   "adh_bruta", "adh_neta", "utilizacion", "productividad", "infoe",
                   "llamadas", "tmo_seg"]].rename(columns={
        "fecha": "Fecha", "nombre": "Agente", "dni": "DNI", "centro": "Centro", "pais": "Pais",
        "modo": "Modo", "supervisor_nombre": "Supervisor", "adh_bruta": "ADH Bruta %",
        "adh_neta": "ADH Neta %", "utilizacion": "Utilizacion %", "productividad": "Productividad %",
        "infoe": "INFOE %", "llamadas": "Llamadas", "tmo_seg": "TMO (seg)"})

    res_ag = df.groupby(["nombre", "centro", "pais", "modo", "supervisor_nombre"]).apply(kpis).reset_index()
    res_ag = res_ag.rename(columns={"nombre": "Agente", "centro": "Centro", "pais": "Pais",
                                    "modo": "Modo", "supervisor_nombre": "Supervisor"})
    res_pais = df.groupby("pais").apply(kpis).reset_index().rename(columns={"pais": "Pais"})
    res_modo = df.groupby("modo").apply(kpis).reset_index().rename(columns={"modo": "Modo"})
    general = kpis(df).to_frame(name=f"{d_ini} a {d_fin}").reset_index().rename(columns={"index": "Indicador"})

    import io as _io
    buf = _io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        general.to_excel(xw, sheet_name="Resumen general", index=False)
        res_ag.to_excel(xw, sheet_name="Por agente", index=False)
        res_pais.to_excel(xw, sheet_name="Por pais", index=False)
        res_modo.to_excel(xw, sheet_name="Por modo", index=False)
        detalle.to_excel(xw, sheet_name="Detalle", index=False)
    buf.seek(0)

    from fastapi.responses import StreamingResponse
    nombre = f"adherencia_{d_ini}_a_{d_fin}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


# ============================================================
#  CRM · Importar base de contactos (xlsx/csv) -> crm_contactos
# ============================================================
@app.post("/crm/importar")
async def crm_importar(
    x_api_key: str = Header(None),
    archivo: UploadFile = File(...),
    campana: str = "Endesa",
):
    """Sube una base de contactos (xlsx o csv). Columnas conocidas -> campos; el resto -> datos_extra (jsonb)."""
    check_key(x_api_key)
    engine = get_engine()
    import json as _json

    raw = await archivo.read()
    nombre_arch = (archivo.filename or "").lower()
    if nombre_arch.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(raw), dtype=str)
    else:
        df = pd.read_excel(io.BytesIO(raw), dtype=str)
    df.columns = [str(c).strip() for c in df.columns]

    cc = pd.read_sql(text("SELECT id FROM campanas WHERE nombre=:n"), engine, params={"n": campana})
    id_camp = int(cc["id"][0]) if not cc.empty else None

    conocidas = {"nombre", "documento", "telefono", "telefono2", "email", "direccion", "ciudad"}
    real = {c.lower(): c for c in df.columns}
    extra_cols = [c for c in df.columns if c.lower() not in conocidas]

    def val(r, key):
        c = real.get(key)
        if c is None:
            return None
        v = r.get(c)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        s = str(v).strip()
        return s if s and s.lower() != "nan" else None

    filas = []
    for _, r in df.iterrows():
        extra = {}
        for c in extra_cols:
            v = r.get(c)
            if v is not None and not (isinstance(v, float) and pd.isna(v)):
                s = str(v).strip()
                if s and s.lower() != "nan":
                    extra[c] = s
        filas.append({
            "nombre": val(r, "nombre"), "documento": val(r, "documento"),
            "telefono": val(r, "telefono"), "telefono2": val(r, "telefono2"),
            "email": val(r, "email"), "direccion": val(r, "direccion"), "ciudad": val(r, "ciudad"),
            "campana_id": id_camp, "estado": "pendiente",
            "datos_extra": (_json.dumps(extra, ensure_ascii=False) if extra else None),
        })
    if not filas:
        raise HTTPException(400, "El archivo no tenia filas.")

    cols = ["nombre", "documento", "telefono", "telefono2", "email", "direccion", "ciudad",
            "campana_id", "estado", "datos_extra"]
    LOTE = 500
    insertadas = 0
    try:
        with engine.begin() as conn:
            for k in range(0, len(filas), LOTE):
                lote = filas[k:k + LOTE]
                valores = []; params = {}
                for j, f in enumerate(lote):
                    ph = []
                    for c in cols:
                        ph.append(f"cast(:{c}{j} as jsonb)" if c == "datos_extra" else f":{c}{j}")
                        params[f"{c}{j}"] = f[c]
                    valores.append("(" + ", ".join(ph) + ")")
                sql = "INSERT INTO crm_contactos (" + ", ".join(cols) + ") VALUES " + ", ".join(valores)
                conn.execute(text(sql), params)
                insertadas += len(lote)
    except Exception as e:
        raise HTTPException(500, f"Error al importar contactos: {type(e).__name__}: {str(e)[:300]} | ejemplo: {filas[0]}")

    return {"ok": True, "contactos_importados": insertadas,
            "columnas_extra_a_datos_extra": extra_cols, "campana": campana}


# ============================================================
#  Agentes · Importar horas/salario por Excel (cruza por dni)
# ============================================================
@app.post("/agentes/importar-horas")
async def agentes_importar_horas(
    x_api_key: str = Header(None),
    archivo: UploadFile = File(...),
):
    """Sube xlsx/csv con columna 'dni' y 'horas_semanales' y/o 'salario_mensual'. Actualiza agentes por dni."""
    check_key(x_api_key)
    engine = get_engine()
    raw = await archivo.read()
    nombre = (archivo.filename or "").lower()
    if nombre.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(raw), dtype=str)
    else:
        df = pd.read_excel(io.BytesIO(raw), dtype=str)
    df.columns = [str(c).strip().lower() for c in df.columns]

    if "dni" not in df.columns:
        raise HTTPException(400, "El archivo debe tener una columna 'dni'.")
    tiene_h = "horas_semanales" in df.columns
    tiene_s = "salario_mensual" in df.columns
    if not tiene_h and not tiene_s:
        raise HTTPException(400, "El archivo debe tener 'horas_semanales' y/o 'salario_mensual'.")

    def num(v):
        if v is None:
            return None
        s = str(v).strip().replace(".", "").replace(",", ".") if False else str(v).strip().replace(",", ".")
        if s == "" or s.lower() == "nan":
            return None
        try:
            return float(s)
        except Exception:
            return None

    filas = []
    for _, r in df.iterrows():
        dni = str(r.get("dni")).strip() if r.get("dni") is not None else None
        if not dni or dni.lower() == "nan":
            continue
        filas.append({
            "dni": dni,
            "h": num(r.get("horas_semanales")) if tiene_h else None,
            "s": num(r.get("salario_mensual")) if tiene_s else None,
        })
    if not filas:
        raise HTTPException(400, "No se encontraron filas con dni.")

    valores = []; params = {}
    for j, f in enumerate(filas):
        valores.append(f"(:dni{j}, cast(:h{j} as numeric), cast(:s{j} as numeric))")
        params[f"dni{j}"] = f["dni"]; params[f"h{j}"] = f["h"]; params[f"s{j}"] = f["s"]
    sql = f"""
        update agentes a set
          horas_semanales = coalesce(v.h, a.horas_semanales),
          salario_mensual = coalesce(v.s, a.salario_mensual)
        from (values {", ".join(valores)}) as v(dni, h, s)
        where a.dni = v.dni
    """
    with engine.begin() as conn:
        res = conn.execute(text(sql), params)
        actualizados = res.rowcount or 0

    return {"ok": True, "filas_archivo": len(filas), "agentes_actualizados": actualizados,
            "sin_cruce": len(filas) - actualizados}
