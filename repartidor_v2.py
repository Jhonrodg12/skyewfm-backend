"""
repartidor_v2.py — Motor de asignación de calendario (trabajo/descanso) por agente,
respetando reglas propias de cada agente (heredadas de su país o con override).

No sustituye a repartidor.py (v4): convive con él. La idea es que este módulo se
use detrás de un endpoint nuevo (ej. /generar-roster-v2) mientras se valida, y el
endpoint actual (/generar-roster con repartidor.py) siga funcionando exactamente
igual que hoy.

Entradas (desacopladas de cómo se calculó la necesidad — Erlang, headcount manual,
o agentes ya distribuidos por franja):
  - fechas: lista de fechas (date) del período a planear (ej. un mes)
  - necesidad_diaria: dict {fecha: n_agentes_requeridos_ese_dia}
  - agentes: lista de dicts, uno por agente, con las reglas YA RESUELTAS
             (ver fetch_agentes_reglas — vienen de la vista agentes_reglas en Supabase)
  - perfil_horas (opcional): dict {hora: peso} para repartir horas de inicio de turno
             dentro del día, igual que hace repartidor.py hoy (_distribuir_horas)

Salida:
  - asignaciones: lista de dicts {agente, fecha, trabaja (bool), hora_inicio (si trabaja)}
  - resumen: cobertura lograda vs requerida por día, y agentes con déficit de horas
"""

import math
from collections import defaultdict
from datetime import date, timedelta

from ortools.sat.python import cp_model


# ---------------------------------------------------------------------------
# 1) Lectura de reglas por agente (Supabase, vista agentes_reglas creada en el
#    Paso 1). Aislado en su propia función para poder mockearlo en pruebas.
# ---------------------------------------------------------------------------
def fetch_agentes_reglas(engine, campana_id=None):
    """
    Devuelve una lista de dicts: id, dni, nombre, pais, horas_semana,
    dias_descanso_semana, patron_descanso ('consecutivo'|'rotativo'),
    max_dias_consecutivos, largo_turno_horas.

    Si tu tabla 'agentes' tiene columna campana_id, filtra por ella; si no,
    ignora ese parámetro (ajusta el WHERE según tu esquema real).
    """
    import pandas as pd
    from sqlalchemy import text

    if campana_id is not None:
        q = text("SELECT * FROM agentes_reglas WHERE campana_id = :c")
        df = pd.read_sql(q, engine, params={"c": campana_id})
    else:
        df = pd.read_sql(text("SELECT * FROM agentes_reglas"), engine)

    return df.to_dict(orient="records")


# ---------------------------------------------------------------------------
# 2) Helpers de semana ISO (para aplicar "días de descanso por SEMANA" sobre
#    fechas reales, no días de semana genéricos).
# ---------------------------------------------------------------------------
def _semana_de(f: date):
    iso = f.isocalendar()
    return (iso[0], iso[1])  # (año_iso, semana_iso)


def _agrupar_por_semana(fechas):
    semanas = defaultdict(list)
    for f in fechas:
        semanas[_semana_de(f)].append(f)
    for k in semanas:
        semanas[k].sort()
    return semanas


# ---------------------------------------------------------------------------
# 3) Núcleo: asignación de calendario trabajo/descanso vía CP-SAT
# ---------------------------------------------------------------------------
def asignar_calendario(fechas, necesidad_diaria, agentes, max_time_seconds=20.0):
    """
    fechas: list[date], ordenada, típicamente un mes completo (o más, si hace falta
            mirar semanas parciales al inicio/fin para las reglas semanales).
    necesidad_diaria: dict {date: int} — cuántos agentes deben trabajar ese día.
                      Si una fecha no está, se asume 0 (nadie requerido ese día,
                      aunque igual pueden trabajar si conviene la regla semanal).
    agentes: list[dict] con al menos: id, dias_descanso_semana, patron_descanso,
             max_dias_consecutivos. (horas_semana / largo_turno_horas se usan
             solo para el resumen/validación, no como restricción dura aquí.)

    Devuelve (asignaciones, resumen).
    """
    n_dias = len(fechas)
    idx_fecha = {f: i for i, f in enumerate(fechas)}
    semanas = _agrupar_por_semana(fechas)

    m = cp_model.CpModel()

    # x[a][i] = 1 si el agente a TRABAJA el día i
    x = {}
    for a in agentes:
        aid = a["id"]
        for i in range(n_dias):
            x[(aid, i)] = m.NewBoolVar(f"x_{aid}_{i}")

    # --- Restricción semanal: exactamente 'dias_descanso_semana' libres por semana ---
    # (para semanas parciales al borde del período, se prorratea proporcional a
    #  los días de esa semana que sí caen dentro del rango planeado.)
    for a in agentes:
        aid = a["id"]
        descanso_obj = int(a["dias_descanso_semana"])
        for _, dias_semana in semanas.items():
            idxs = [idx_fecha[f] for f in dias_semana]
            trabajo_dias = len(idxs)
            if trabajo_dias == 7:
                dias_trabajo_obj = 7 - descanso_obj
            else:
                # Semana parcial (borde del período): prorratea la proporción
                # de días trabajados esperados en esos días disponibles.
                dias_trabajo_obj = round(trabajo_dias * (7 - descanso_obj) / 7)
            m.Add(sum(x[(aid, i)] for i in idxs) == dias_trabajo_obj)

    # --- Restricción: patrón CONSECUTIVO de descanso (si aplica) ---
    # Se modela así: si descanso_obj == 2 y patron == 'consecutivo', los 2 días
    # libres de esa semana deben ser adyacentes (ej. sáb+dom, o cualquier par
    # consecutivo permitido). Generalizado a 'descanso_obj' días libres seguidos.
    for a in agentes:
        aid = a["id"]
        if a.get("patron_descanso") != "consecutivo":
            continue
        descanso_obj = int(a["dias_descanso_semana"])
        for _, dias_semana in semanas.items():
            idxs = [idx_fecha[f] for f in dias_semana]
            if len(idxs) < 7:
                continue  # solo aplicamos el patrón estricto en semanas completas
            # Variable: para cada posible "inicio de bloque libre" de tamaño
            # descanso_obj dentro de la semana, un booleano; exactamente uno activo.
            n = len(idxs)
            bloques = []
            for start in range(n - descanso_obj + 1):
                b = m.NewBoolVar(f"bloque_{aid}_{idxs[0]}_{start}")
                # si b=1, todos los días del bloque son descanso (x=0) y el resto trabajo (x=1)
                for k in range(descanso_obj):
                    m.Add(x[(aid, idxs[start + k])] == 0).OnlyEnforceIf(b)
                bloques.append(b)
            m.Add(sum(bloques) == 1)

    # --- Restricción: máximo de días consecutivos trabajados ---
    for a in agentes:
        aid = a["id"]
        max_cons = int(a["max_dias_consecutivos"])
        for i in range(n_dias - max_cons):
            ventana = [x[(aid, i + k)] for k in range(max_cons + 1)]
            m.Add(sum(ventana) <= max_cons)

    # --- Cobertura diaria: cubrir la necesidad, con holgura penalizada si no
    #     es posible cubrir exacto (evita que el modelo sea infactible) ---
    deficit = {}
    exceso = {}
    for i, f in enumerate(fechas):
        req = int(necesidad_diaria.get(f, 0))
        total_dia = sum(x[(a["id"], i)] for a in agentes)
        d = m.NewIntVar(0, len(agentes), f"deficit_{i}")
        e = m.NewIntVar(0, len(agentes), f"exceso_{i}")
        m.Add(total_dia - req == e - d)
        deficit[i] = d
        exceso[i] = e

    # Objetivo: minimizar déficit primero (fuertemente penalizado) y exceso después
    m.Minimize(sum(deficit.values()) * 1000 + sum(exceso.values()))

    sv = cp_model.CpSolver()
    sv.parameters.max_time_in_seconds = max_time_seconds
    sv.parameters.num_search_workers = 4
    status = sv.Solve(m)

    asignaciones = []
    for a in agentes:
        aid = a["id"]
        for i, f in enumerate(fechas):
            trabaja = bool(sv.Value(x[(aid, i)]))
            asignaciones.append({"agente_id": aid, "fecha": f, "trabaja": trabaja})

    resumen_dias = []
    for i, f in enumerate(fechas):
        req = int(necesidad_diaria.get(f, 0))
        cubiertos = sum(1 for a in agentes if sv.Value(x[(a["id"], i)]))
        resumen_dias.append({"fecha": f, "requerido": req, "cubierto": cubiertos,
                              "deficit": max(0, req - cubiertos)})

    return asignaciones, {
        "status": sv.StatusName(status),
        "por_dia": resumen_dias,
        "deficit_total": sum(d["deficit"] for d in resumen_dias),
    }


# ---------------------------------------------------------------------------
# 4) Hora de inicio de turno dentro del día (reutiliza la idea de repartidor.py)
# ---------------------------------------------------------------------------
def _distribuir_horas(n_agentes, perfil_horas):
    """Idéntica lógica a repartidor.py::_distribuir_horas — reparte proporcional
    al perfil de demanda horaria, preservando el total exacto de agentes."""
    if not perfil_horas or n_agentes == 0:
        return [8] * n_agentes
    total_peso = sum(perfil_horas.values())
    if total_peso == 0:
        return [8] * n_agentes
    asignados = {}
    horas_ordenadas = sorted(perfil_horas.keys())
    for h in horas_ordenadas:
        cuota = perfil_horas[h] / total_peso * n_agentes
        asignados[h] = int(round(cuota))
    diff = n_agentes - sum(asignados.values())
    if diff != 0:
        orden_peso = sorted(perfil_horas, key=lambda h: perfil_horas[h], reverse=True)
        i = 0
        while diff != 0 and orden_peso:
            h = orden_peso[i % len(orden_peso)]
            if diff > 0:
                asignados[h] += 1
                diff -= 1
            elif asignados.get(h, 0) > 0:
                asignados[h] -= 1
                diff += 1
            i += 1
    horas_lista = []
    for h in horas_ordenadas:
        horas_lista += [h] * asignados.get(h, 0)
    return horas_lista


def asignar_horas_inicio(asignaciones, perfil_horas_por_dia):
    """
    Añade 'hora_inicio' a cada asignación con trabaja=True, repartiendo según
    el perfil de demanda de ESE día (perfil_horas_por_dia: dict {fecha: {hora: peso}}).
    """
    por_dia = defaultdict(list)
    for asg in asignaciones:
        if asg["trabaja"]:
            por_dia[asg["fecha"]].append(asg)

    for f, lista in por_dia.items():
        perfil = perfil_horas_por_dia.get(f, {})
        horas = _distribuir_horas(len(lista), perfil)
        for asg, h in zip(lista, horas):
            asg["hora_inicio"] = h

    return asignaciones
