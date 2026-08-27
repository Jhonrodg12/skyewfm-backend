"""
Repartidor v4 — todos los Multiskill con turno, distribuidos según tráfico.
- NO deja backup: todos los agentes reciben turno.
- Distribuye las horas de inicio proporcionalmente al perfil de demanda.
- Mantiene descansos rotativos (2 días libres por semana).
"""
import math
from collections import defaultdict

NOM = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
LIBRES = {p: {p, (p + 1) % 7} for p in range(7)}


def demanda_diaria(S):
    ct = defaultdict(int)
    for k, n in S["xc"].items():
        t, p = map(int, k.split("_"))
        ct[t] += n
    et = defaultdict(int)
    for k, n in S["xe"].items():
        et[int(k)] += n
    return dict(ct), dict(et)


def _distribuir_horas(n_agentes, perfil_horas):
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
                asignados[h] += 1; diff -= 1
            elif asignados.get(h, 0) > 0:
                asignados[h] -= 1; diff += 1
            i += 1
    horas_lista = []
    for h in horas_ordenadas:
        horas_lista += [h] * asignados.get(h, 0)
    return horas_lista


def repartir_todos(agentes, perfil_horas, dias_cubiertos, etiqueta, rota_libres=True):
    n_ag = len(agentes)
    if n_ag == 0:
        return [], {"etiqueta": etiqueta, "asignados": 0, "backup": 0, "total_dia": 0}
    horas_lista = _distribuir_horas(n_ag, perfil_horas)
    patrones = list(range(7)) if rota_libres else [5]
    asign = []
    for i in range(n_ag):
        hora = horas_lista[i] if i < len(horas_lista) else horas_lista[i % len(horas_lista)]
        patron = patrones[i % len(patrones)] if rota_libres else 5
        asign.append({"agente": agentes[i], "hora_inicio": hora,
                       "libres": LIBRES[patron], "patron": patron})
    cob = {d: 0 for d in range(7)}
    for a in asign:
        for d in dias_cubiertos:
            if d not in a["libres"]:
                cob[d] += 1
    resumen = {"etiqueta": etiqueta, "asignados": len(asign), "backup": 0,
               "cobertura_por_dia": cob}
    return asign, resumen
