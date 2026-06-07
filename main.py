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
    # Solo agentes MULTISKILL reciben turno del optimizador (backoffice se asigna aparte)
    ag = pd.read_sql("SELECT id,nombre,centro,pais,jornada_horas,modo FROM agentes WHERE estado='ACTIVO' AND UPPER(modo)='MULTISKILL' ORDER BY id", engine)
    esp = ag[ag["pais"] == "España"].to_dict("records")
    col = ag[ag["pais"] == "Colombia"].to_dict("records")
    # Perfil de tráfico por hora (turnos que pidió el optimizador) — define dónde arrancan más agentes
    col_t, esp_t = repartidor.demanda_diaria(S)
    # Repartir TODOS los Multiskill (sin backup), según el perfil de tráfico
    base_col, res_col = repartidor.repartir_todos(col, col_t, set(range(7)), "Colombia", True)
    base_esp, res_esp = repartidor.repartir_todos(esp, esp_t, {0, 1, 2, 3, 4}, "España", False)
    base_todos = base_col + base_esp

    # 3) Registrar turnos usados
    dias_mes = pd.date_range(f"{mes}-01", pd.Timestamp(f"{mes}-01") + pd.offsets.MonthEnd(0))
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

    # 4) Generar asignaciones
    filas = []
    for item in base_todos:
        a = item["agente"]; hi = item["hora_inicio"]; libres = item["libres"]
        hora_fin = (hi + largo) % 24; tid = turno_id_por_hora[hi]
        for d in dias_mes:
            if d.dayofweek in libres:
                filas.append({"agente_id": int(a["id"]), "fecha": d.date(), "campana_id": int(id_campana),
                              "turno_id": None, "hora_inicio": None, "hora_fin": None, "tipo": "libre", "creado_por": "optimizador-web"})
            else:
                filas.append({"agente_id": int(a["id"]), "fecha": d.date(), "campana_id": int(id_campana),
                              "turno_id": int(tid), "hora_inicio": f"{hi:02d}:00", "hora_fin": f"{hora_fin:02d}:00", "tipo": "trabajo", "creado_por": "optimizador-web"})
    df_asig = pd.DataFrame(filas)

    # 5) Cargar asignaciones (limpiar mes antes)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM asignaciones WHERE campana_id=:c AND fecha>=:i AND fecha<=:f"),
                     {"c": int(id_campana), "i": dias_mes[0].date(), "f": dias_mes[-1].date()})
    df_asig.to_sql("asignaciones", engine, if_exists="append", index=False, method="multi", chunksize=500)

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
            filas_b.append({"asignacion_id": int(r["asignacion_id"]), "hora_inicio": bhi, "duracion_min": dm, "tipo": tp})
    df_b = pd.DataFrame(filas_b)
    ids = [int(x) for x in asg["asignacion_id"].tolist()]
    with engine.begin() as conn:
        if ids:
            conn.execute(text("DELETE FROM breaks WHERE asignacion_id = ANY(:ids)"), {"ids": ids})
    if not df_b.empty:
        df_b.to_sql("breaks", engine, if_exists="append", index=False, method="multi", chunksize=500)

    return {
        "ok": True,
        "mes": mes,
        "volumen_total": S["total"],
        "asignaciones": len(df_asig),
        "breaks": len(df_b),
        "colombia": res_col,
        "espana": res_esp,
    }
