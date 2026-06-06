"""
Repartidor v3 — reparto parejo con libres rotando.
Convierte el resultado del optimizador (S) en asignaciones por persona.
"""
import math
from collections import defaultdict, Counter

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


def repartir(agentes, turnos_por_hora, dias_cubiertos, etiqueta, rota_libres=True):
    n_ag = len(agentes)
    total_dia = sum(turnos_por_hora.values())
    if n_ag == 0 or total_dia == 0:
        return [], list(agentes), {"etiqueta": etiqueta, "asignados": 0, "backup": n_ag, "deficit": 0}
    horas_lista = []
    for h, n in sorted(turnos_por_hora.items()):
        horas_lista += [h] * n
    if rota_libres:
        dias_trab = 5
        patrones = list(range(7))
    else:
        dias_trab = len(dias_cubiertos)
        patrones = [5]
    personas_por_plaza = len(dias_cubiertos) / dias_trab
    objetivo = min(n_ag, math.ceil(len(horas_lista) * personas_por_plaza))
    asign = []
    for i in range(objetivo):
        hora = horas_lista[i % len(horas_lista)]
        patron = patrones[i % len(patrones)] if rota_libres else 5
        asign.append({"agente": agentes[i], "hora_inicio": hora,
                      "libres": LIBRES[patron], "patron": patron})
    backup = [agentes[i] for i in range(objetivo, n_ag)]
    cob = {d: 0 for d in range(7)}
    for a in asign:
        for d in dias_cubiertos:
            if d not in a["libres"]:
                cob[d] += 1
    deficit = sum(max(0, total_dia - cob[d]) for d in dias_cubiertos)
    resumen = {"etiqueta": etiqueta, "asignados": len(asign),
               "backup": len(backup), "total_dia": total_dia, "deficit": deficit}
    return asign, backup, resumen
