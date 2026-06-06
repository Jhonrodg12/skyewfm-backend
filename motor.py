import io, math
import numpy as np
import pandas as pd
import holidays
from ortools.sat.python import cp_model

NOM = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
LIBRES = {p: {p, (p + 1) % 7} for p in range(7)}


def erlang_b(a, c):
    b = 1.0
    for k in range(1, a + 1):
        b = (c * b) / (k + c * b)
    return b


def erlang_c(a, c):
    b = erlang_b(a, c); r = c / a
    return b / (1 - r + r * b)


def nivel_servicio(a, c, aht, asa):
    return 0.0 if a <= c else 1 - erlang_c(a, c) * math.exp(-(a - c) * asa / aht)


def nivel_atencion(ag, carga, aht, pac, NMAX=250):
    if carga <= 0:
        return 1.0
    lam = carga / aht; mu = 1.0 / aht; theta = 1.0 / pac
    p = [1.0]
    for n in range(1, NMAX + 1):
        muerte = n * mu if n <= ag else ag * mu + (n - ag) * theta
        p.append(p[-1] * lam / muerte)
    S = sum(p)
    aband = sum(p[n] * max(0, (n - ag)) * theta for n in range(len(p))) / S
    return 1 - aband / lam


def largo_desde_historico(file_bytes, mes, scope, K, semanas, ajuste=0.0, mixto=False):
    df = pd.read_csv(io.BytesIO(file_bytes))
    df.columns = [c.strip().lower() for c in df.columns]
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    df["hora"] = pd.to_numeric(df["hora"], errors="coerce")
    volcol = "entrantes" if "entrantes" in df.columns else "vol"
    df[volcol] = pd.to_numeric(df[volcol], errors="coerce").fillna(0)
    df = df.dropna(subset=["fecha", "hora"])
    df["hora"] = df["hora"].astype(int)
    hh = df.groupby(["fecha", "hora"])[volcol].sum().reset_index()
    hh.columns = ["fecha", "hora", "vol"]
    dia = hh.groupby("fecha")["vol"].sum()
    real_piv = hh.pivot_table(index="fecha", columns="hora", values="vol", aggfunc="sum")
    real_set = set(dia.index)

    if scope == "Cataluña (Barcelona)":
        ES = holidays.Spain(years=range(2023, 2028), subdiv="CT")
    else:
        ES = holidays.Spain(years=range(2023, 2028))

    def fest(t):
        return t.date() in ES

    def total_diario(t):
        wd = 6 if fest(t) else t.dayofweek
        s = dia[(dia.index < t) & (dia.index.dayofweek == wd)]
        if wd != 6:
            s = s[[not fest(d) for d in s.index]]
        return s.tail(K).mean()

    ini = pd.Timestamp(mes + "-01")
    fin_datos = hh["fecha"].max()
    ref = min(ini, fin_datos + pd.Timedelta(days=1))
    win = hh[(hh["fecha"] < ref) & (hh["fecha"] >= ref - pd.Timedelta(weeks=semanas))]
    win = win[[not fest(d) for d in win["fecha"]]]
    if win.empty:
        win = hh[hh["fecha"] >= fin_datos - pd.Timedelta(weeks=semanas)]
        win = win[[not fest(d) for d in win["fecha"]]]
    perfil = win.groupby([win["fecha"].dt.dayofweek, "hora"])["vol"].mean().unstack(fill_value=0)
    perfil = perfil.div(perfil.sum(axis=1), axis=0)

    filas = []
    for t in pd.date_range(ini, ini + pd.offsets.MonthEnd(0)):
        wd = 6 if fest(t) else t.dayofweek
        if mixto and t in real_set:
            row = real_piv.loc[t] if t in real_piv.index else None
            for hr in range(24):
                vv = row.get(hr) if row is not None else None
                filas.append({"fecha": t, "intervalo": hr, "volumen": int(vv) if pd.notna(vv) else 0})
        else:
            dt = total_diario(t)
            for hr in range(24):
                filas.append({"fecha": t, "intervalo": hr, "volumen": round(dt * perfil.loc[wd].get(hr, 0) * (1 + ajuste))})
    return pd.DataFrame(filas)


def dimension_roster(largo, AHT, SLA, ASA, OCC, UTL, ESP_MAX, LARGO, NDA_OBJ, PACIENCIA, estructura="mixto"):
    largo = largo.copy()
    largo["dow"] = pd.to_datetime(largo["fecha"]).dt.dayofweek
    volmax = {dw: [0.0] * 24 for dw in range(7)}
    for (dw, h), g in largo.groupby(["dow", "intervalo"]):
        volmax[dw][int(h)] = float(g["volumen"].max())
    peak = {dw: [0] * 24 for dw in range(7)}
    occ = {dw: [0.0] * 24 for dw in range(7)}
    nda = {dw: [1.0] * 24 for dw in range(7)}
    for dw in range(7):
        for h in range(24):
            ca = volmax[dw][h] * AHT / 3600
            if ca <= 0:
                continue
            a = int(ca) + 1
            while nivel_servicio(a, ca, AHT, ASA) < SLA:
                a += 1
            en = max(a, math.ceil(ca / OCC))
            while nivel_atencion(en, ca, AHT, PACIENCIA) < NDA_OBJ:
                en += 1
            peak[dw][h] = math.ceil(en / UTL)
            occ[dw][h] = ca / en
            nda[dw][h] = nivel_atencion(en, ca, AHT, PACIENCIA)

    H = 24
    turnos = {ini: [(ini + k) % H for k in range(LARGO)] for ini in range(H)}
    tiene_flex = estructura in ("mixto", "flex")
    tiene_fijo = estructura in ("mixto", "fijo")
    m = cp_model.CpModel()
    xc = {(t, p): m.NewIntVar(0, 300, f"c{t}_{p}") for t in turnos for p in range(7)} if tiene_flex else {}
    xe = {t: m.NewIntVar(0, 300, f"e{t}") for t in turnos} if tiene_fijo else {}
    weekend_sin_cubrir = 0
    for dw in range(7):
        for h in range(H):
            req = peak[dw][h]
            if req <= 0:
                continue
            if not tiene_flex and dw in (5, 6):
                weekend_sin_cubrir += req
                continue
            col = sum(xc[(t, p)] for t in turnos for p in range(7) if dw not in LIBRES[p] and h in turnos[t]) if tiene_flex else 0
            esp = sum(xe[t] for t in turnos if dw not in LIBRES[5] and h in turnos[t]) if tiene_fijo else 0
            m.Add(col + esp >= req)
    if estructura == "mixto":
        m.Add(sum(xe.values()) <= int(ESP_MAX))
    objetivo = 0
    if tiene_flex:
        objetivo = objetivo + sum(xc.values()) * 100
    if tiene_fijo:
        objetivo = objetivo + (-sum(xe.values()) if estructura == "mixto" else sum(xe.values()))
    m.Minimize(objetivo)
    sv = cp_model.CpSolver(); sv.Solve(m)

    xe_s = {str(t): sv.Value(xe[t]) for t in xe if sv.Value(xe[t]) > 0}
    xc_s = {f"{t}_{p}": sv.Value(xc[(t, p)]) for (t, p) in xc if sv.Value(xc[(t, p)]) > 0}
    te = sum(xe_s.values()); tc = sum(xc_s.values())

    return {"peak": peak, "occ": occ, "nda": nda,
            "xe": xe_s, "xc": xc_s, "te": te, "tc": tc, "estructura": estructura,
            "weekend_sin_cubrir": weekend_sin_cubrir,
            "total": int(largo["volumen"].sum())}

