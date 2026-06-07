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
    #    Todo en UNA transacción: o se escribe completo, o no se toca nada (evita rosters a medias).
    #    Usamos la lista 'filas' directamente (tipos nativos: None real e int real).
    #    Insertamos por LOTES de 500 para no exceder el límite de parámetros del driver.
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM asignaciones WHERE campana_id=:c AND fecha>=:i AND fecha<=:f AND (bloqueado IS NULL OR bloqueado = false)"),
                         {"c": int(id_campana), "i": dias_mes[0].date(), "f": dias_mes[-1].date()})
            ins = text("""
                INSERT INTO asignaciones (agente_id, fecha, campana_id, turno_id, hora_inicio, hora_fin, tipo, creado_por)
                VALUES (:agente_id, :fecha, :campana_id, :turno_id, :hora_inicio, :hora_fin, :tipo, :creado_por)
            """)
            for k in range(0, len(filas), 1000):
                conn.execute(ins, filas[k:k+1000])
    except Exception as e:
        ejemplo = filas[0] if filas else {}
        raise HTTPException(500, f"Error al escribir asignaciones: {type(e).__name__}: {str(e)[:300]} | ejemplo fila: {ejemplo}")

    # 6) Generar y cargar breaks
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
        with engine.begin() as conn:
            if ids:
                conn.execute(text("DELETE FROM breaks WHERE asignacion_id = ANY(:ids)"),
                             {"ids": ids})
            if filas_b:
                ins_b = text("""
                    INSERT INTO breaks (asignacion_id, hora_inicio, duracion_min, tipo)
                    VALUES (:asignacion_id, :hora_inicio, :duracion_min, :tipo)
                """)
                for k in range(0, len(filas_b), 1000):
                    conn.execute(ins_b, filas_b[k:k+1000])
    except Exception as e:
        ejemplo = filas_b[0] if filas_b else {}
        raise HTTPException(500, f"Error al escribir breaks: {type(e).__name__}: {str(e)[:300]} | ejemplo: {ejemplo}")

    return {
        "ok": True,
        "mes": mes,
        "volumen_total": S["total"],
        "asignaciones": n_asig,
        "breaks": len(filas_b),
        "colombia": res_col,
        "espana": res_esp,
    }
