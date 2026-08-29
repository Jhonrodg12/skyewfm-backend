"""
Skye WFM · Generación de documentos laborales
=============================================

Comprobante de pago (desprendible) y certificado laboral en PDF.

Principio de diseño: **la base decide qué dice el documento, este módulo
solo decide cómo se ve.** El contenido llega ya armado y congelado desde
`core.datos_comprobante()` y `core.certificado_laboral.contenido`, así que
un certificado emitido hoy sigue siendo reproducible aunque mañana cambie
el salario, la plantilla o esta misma librería.

Dependencias:
    pip install reportlab httpx sqlalchemy psycopg2-binary qrcode[pil]

Variables de entorno:
    SUPABASE_URL              https://<ref>.supabase.co
    SUPABASE_SERVICE_KEY      clave service_role (nunca la publicable)
    DATABASE_URL              cadena de conexión a Postgres
    SKYE_BUCKET_DOCUMENTOS    nombre del bucket privado (por defecto: documentos)
"""

from __future__ import annotations

import io
import os
from datetime import date, datetime
from typing import Any

import httpx
import qrcode
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import create_engine, text

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BUCKET = os.environ.get("SKYE_BUCKET_DOCUMENTOS", "documentos")

engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)

VERDE = colors.HexColor("#0E7C6B")
TINTA = colors.HexColor("#101614")
GRIS = colors.HexColor("#59635E")
LINEA = colors.HexColor("#D3D9D2")

MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


# --------------------------------------------------------------------------
# utilidades
# --------------------------------------------------------------------------
def money(valor: Any, moneda: str = "COP") -> str:
    """Formatea un importe. COP sin decimales, el resto con dos."""
    if valor is None:
        return ""
    v = float(valor)
    if moneda == "COP":
        return f"$ {v:,.0f}".replace(",", ".")
    return f"{v:,.2f} {moneda}".replace(",", "@").replace(".", ",").replace("@", ".")


def fecha_larga(d: Any) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return f"{d.day} de {MESES[d.month - 1]} de {d.year}"


def _estilos():
    ss = getSampleStyleSheet()
    return {
        "titulo": ParagraphStyle(
            "titulo", parent=ss["Normal"], fontName="Helvetica-Bold",
            fontSize=15, leading=19, textColor=TINTA, spaceAfter=2,
        ),
        "sub": ParagraphStyle(
            "sub", parent=ss["Normal"], fontName="Helvetica",
            fontSize=9, leading=12, textColor=GRIS,
        ),
        "cuerpo": ParagraphStyle(
            "cuerpo", parent=ss["Normal"], fontName="Helvetica",
            fontSize=10.5, leading=16, textColor=TINTA, alignment=TA_JUSTIFY,
        ),
        "centro": ParagraphStyle(
            "centro", parent=ss["Normal"], fontName="Helvetica-Bold",
            fontSize=13, leading=17, textColor=TINTA, alignment=TA_CENTER,
        ),
        "pie": ParagraphStyle(
            "pie", parent=ss["Normal"], fontName="Helvetica",
            fontSize=7.5, leading=10, textColor=GRIS,
        ),
    }


def _qr(texto: str, lado_mm: float = 24) -> Image:
    img = qrcode.make(texto)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Image(buf, width=lado_mm * mm, height=lado_mm * mm)


# --------------------------------------------------------------------------
# almacenamiento
# --------------------------------------------------------------------------
def subir(ruta: str, contenido: bytes, content_type: str = "application/pdf") -> str:
    """Sube al bucket privado. Devuelve la ruta guardada."""
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{ruta}"
    r = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {SERVICE_KEY}",
            "apikey": SERVICE_KEY,
            "Content-Type": content_type,
            "x-upsert": "true",
        },
        content=contenido,
        timeout=60,
    )
    r.raise_for_status()
    return ruta


def url_firmada(ruta: str, segundos: int = 300) -> str:
    """URL temporal para descargar. Nunca se sirve el bucket como público."""
    url = f"{SUPABASE_URL}/storage/v1/object/sign/{BUCKET}/{ruta}"
    r = httpx.post(
        url,
        headers={"Authorization": f"Bearer {SERVICE_KEY}", "apikey": SERVICE_KEY},
        json={"expiresIn": segundos},
        timeout=30,
    )
    r.raise_for_status()
    return SUPABASE_URL + "/storage/v1" + r.json()["signedURL"]


# --------------------------------------------------------------------------
# comprobante de pago
# --------------------------------------------------------------------------
def generar_comprobante(liquidacion_id: str) -> dict:
    with engine.begin() as cx:
        datos = cx.execute(
            text("select core.datos_comprobante(:id)"), {"id": liquidacion_id}
        ).scalar_one()

    if not datos:
        raise ValueError(f"La liquidación {liquidacion_id} no existe")

    emp, tra = datos["empleador"], datos["trabajador"]
    per, res = datos["periodo"], datos["resumen"]
    moneda = res.get("moneda", "COP")
    st = _estilos()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Comprobante {per['nombre']} · {tra['nombres']} {tra['apellidos']}",
        author=emp["nombre"],
    )
    el: list = []

    # cabecera
    el.append(Table(
        [[
            Paragraph(f"<b>{emp['nombre']}</b><br/>"
                      f"<font size=8 color='#59635E'>NIT {emp.get('identificacion') or '—'}<br/>"
                      f"{emp.get('direccion') or ''}<br/>{emp.get('ciudad') or ''}</font>", st["cuerpo"]),
            Paragraph("<b>COMPROBANTE DE PAGO DE NÓMINA</b><br/>"
                      f"<font size=9 color='#59635E'>{per['nombre']}<br/>"
                      f"Del {fecha_larga(per['desde'])} al {fecha_larga(per['hasta'])}</font>",
                      ParagraphStyle("d", parent=st["cuerpo"], alignment=2)),
        ]],
        colWidths=[95 * mm, 85 * mm],
        style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 1.2, VERDE),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 10),
        ]),
    ))
    el.append(Spacer(1, 10))

    # datos del trabajador
    filas = [
        ["Trabajador", f"{tra['nombres']} {tra['apellidos']}",
         "Documento", f"{tra.get('tipo_documento') or 'CC'} {tra['documento']}"],
        ["Cargo", tra.get("cargo") or "—",
         "Ingreso", fecha_larga(tra["fecha_ingreso"]) if tra.get("fecha_ingreso") else "—"],
        ["EPS", tra.get("eps") or "—", "Fondo de pensión", tra.get("afp") or "—"],
        ["Pago en", tra.get("banco") or "—", "Centro de costos", tra.get("centro_costos") or "—"],
    ]
    el.append(Table(
        filas, colWidths=[26 * mm, 64 * mm, 30 * mm, 60 * mm],
        style=TableStyle([
            ("FONT", (0, 0), (-1, -1), "Helvetica", 8.8),
            ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 8.8),
            ("FONT", (2, 0), (2, -1), "Helvetica-Bold", 8.8),
            ("TEXTCOLOR", (0, 0), (0, -1), GRIS),
            ("TEXTCOLOR", (2, 0), (2, -1), GRIS),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINEA),
        ]),
    ))
    el.append(Spacer(1, 14))

    # devengados y deducidos
    dev = [l for l in datos["lineas"] if l["clase"] == "devengo"]
    ded = [l for l in datos["lineas"] if l["clase"] == "deduccion"]
    info = [l for l in datos["lineas"] if l["clase"] == "informativo"]

    def bloque(titulo: str, lineas: list, total: Any) -> Table:
        filas = [[titulo, "Cantidad", "Valor"]]
        for l in lineas:
            cant = ""
            if l.get("cantidad") is not None:
                cant = f"{float(l['cantidad']):g}"
                if l.get("unidad"):
                    cant += f" {l['unidad']}"
            filas.append([l["descripcion"], cant, money(l["valor"], moneda)])
        filas.append(["Total", "", money(total, moneda)])
        return Table(
            filas, colWidths=[52 * mm, 22 * mm, 26 * mm],
            style=TableStyle([
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
                ("TEXTCOLOR", (0, 0), (-1, 0), GRIS),
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, LINEA),
                ("FONT", (0, 1), (-1, -2), "Helvetica", 9),
                ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 9.5),
                ("LINEABOVE", (0, -1), (-1, -1), 0.8, LINEA),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]),
        )

    el.append(Table(
        [[bloque("DEVENGADO", dev, res["total_devengado"]),
          bloque("DEDUCIDO", ded, res["total_deducido"])]],
        colWidths=[100 * mm, 100 * mm],
        style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]),
    ))
    el.append(Spacer(1, 12))

    # neto
    el.append(Table(
        [["NETO A PAGAR", money(res["neto"], moneda)]],
        colWidths=[140 * mm, 40 * mm],
        style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#DCEAE5")),
            ("FONT", (0, 0), (0, 0), "Helvetica-Bold", 10),
            ("FONT", (1, 0), (1, 0), "Helvetica-Bold", 13),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("TEXTCOLOR", (0, 0), (-1, -1), TINTA),
            ("TOPPADDING", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ("LEFTPADDING", (0, 0), (0, 0), 10),
            ("RIGHTPADDING", (1, 0), (1, 0), 10),
        ]),
    ))
    el.append(Spacer(1, 12))

    # detalle de horas: la trazabilidad de los recargos
    if info:
        el.append(Paragraph(
            "<b>Detalle del tiempo trabajado</b><br/>" + info[0]["descripcion"] +
            f"<br/>Días con turno: {res['dias_laborados']} · "
            f"Valor hora ordinaria: {money(res['valor_hora'], moneda)}",
            st["pie"]))
        el.append(Spacer(1, 6))

    el.append(Paragraph(
        "Los recargos de este comprobante se calcularon contando el tiempo real de cada turno "
        "programado, según la hora de inicio nocturna y los porcentajes vigentes en el periodo. "
        "Documento generado automáticamente; conserva validez sin firma manuscrita.",
        st["pie"]))

    doc.build(el)
    pdf = buf.getvalue()

    ruta = f"comprobantes/{per['desde'][:7]}/{tra['documento']}-{liquidacion_id}.pdf"
    subir(ruta, pdf)

    with engine.begin() as cx:
        cx.execute(text("""
            insert into core.comprobante_nomina (organizacion_id, liquidacion_id, ruta_storage)
            select l.organizacion_id, l.id, :ruta from core.liquidacion l where l.id = :lid
            on conflict (liquidacion_id) do update
              set ruta_storage = excluded.ruta_storage, generado_en = now()
        """), {"ruta": ruta, "lid": liquidacion_id})

    return {"ruta": ruta, "url": url_firmada(ruta), "bytes": len(pdf)}


# --------------------------------------------------------------------------
# certificado laboral
# --------------------------------------------------------------------------
def generar_certificado(certificado_id: str, url_verificacion: str | None = None) -> dict:
    with engine.begin() as cx:
        fila = cx.execute(text("""
            select contenido, codigo_verificacion, incluye_salario, incluye_funciones
            from core.certificado_laboral where id = :id
        """), {"id": certificado_id}).mappings().one_or_none()

    if fila is None:
        raise ValueError(f"El certificado {certificado_id} no existe")

    c = fila["contenido"]
    emp, tra, vin = c["empleador"], c["trabajador"], c["vinculo"]
    codigo = fila["codigo_verificacion"]
    st = _estilos()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=25 * mm, rightMargin=25 * mm,
        topMargin=22 * mm, bottomMargin=20 * mm,
        title=f"Certificado laboral · {tra['nombres']} {tra['apellidos']}",
        author=emp["nombre"],
    )
    el: list = []

    el.append(Paragraph(f"<b>{emp['nombre']}</b>", st["titulo"]))
    el.append(Paragraph(
        f"NIT {emp.get('identificacion') or '—'} · {emp.get('direccion') or ''} · "
        f"{emp.get('ciudad') or ''}{' · ' + emp['telefono'] if emp.get('telefono') else ''}",
        st["sub"]))
    el.append(Spacer(1, 22))

    el.append(Paragraph("EL DEPARTAMENTO DE GESTIÓN HUMANA", st["centro"]))
    el.append(Paragraph("CERTIFICA QUE:", st["centro"]))
    el.append(Spacer(1, 20))

    salario_txt = ""
    if fila["incluye_salario"] and c.get("salario"):
        s = c["salario"]
        salario_txt = (f", devengando una asignación básica mensual de "
                       f"<b>{money(s['valor'], s.get('moneda', 'COP'))}</b>")

    anios, meses = divmod(int(vin.get("meses_servicio") or 0), 12)
    tiempo = []
    if anios:
        tiempo.append(f"{anios} año{'s' if anios != 1 else ''}")
    if meses:
        tiempo.append(f"{meses} mes{'es' if meses != 1 else ''}")
    tiempo_txt = " y ".join(tiempo) if tiempo else "menos de un mes"

    el.append(Paragraph(
        f"El(la) señor(a) <b>{tra['nombres']} {tra['apellidos']}</b>, identificado(a) con "
        f"{tra.get('tipo_documento') or 'CC'} número <b>{tra['documento']}</b>, labora en esta "
        f"empresa desde el <b>{fecha_larga(vin['fecha_ingreso'])}</b>, es decir, un tiempo de "
        f"servicio de <b>{tiempo_txt}</b>, desempeñando el cargo de <b>{vin.get('cargo') or '—'}</b>, "
        f"mediante contrato de trabajo a término <b>{vin.get('tipo_contrato') or '—'}</b>, con una "
        f"jornada de {vin.get('jornada_horas_semana')} horas semanales{salario_txt}.",
        st["cuerpo"]))
    el.append(Spacer(1, 12))

    if fila["incluye_funciones"] and c.get("funciones"):
        el.append(Paragraph(
            f"Las funciones desempeñadas son las siguientes: {c['funciones']}", st["cuerpo"]))
        el.append(Spacer(1, 12))

    if c.get("dirigido_a"):
        el.append(Paragraph(
            f"La presente certificación se expide a solicitud del interesado(a) y se dirige a "
            f"<b>{c['dirigido_a']}</b>.", st["cuerpo"]))
    else:
        el.append(Paragraph(
            "La presente certificación se expide a solicitud del interesado(a).", st["cuerpo"]))

    el.append(Spacer(1, 16))
    el.append(Paragraph(
        f"Dada en {c.get('ciudad') or emp.get('ciudad') or ''} a los "
        f"{fecha_larga(c['emitido_en'])}.", st["cuerpo"]))
    el.append(Spacer(1, 40))

    verificacion = url_verificacion or f"Código de verificación: {codigo}"
    el.append(KeepTogether(Table(
        [[
            Paragraph(
                "_____________________________________<br/><br/>"
                f"<b>{emp.get('firmante') or ''}</b><br/>"
                f"<font size=9 color='#59635E'>{emp.get('firmante_cargo') or ''}<br/>"
                f"{emp['nombre']}</font>", st["cuerpo"]),
            _qr(verificacion),
        ]],
        colWidths=[125 * mm, 35 * mm],
        style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]),
    )))
    el.append(Spacer(1, 14))
    el.append(Paragraph(
        f"Código de verificación <b>{codigo}</b>. Este certificado puede comprobarse en línea. "
        "Documento generado automáticamente por el sistema de gestión humana.",
        st["pie"]))

    doc.build(el)
    pdf = buf.getvalue()

    ruta = f"certificados/{tra['documento']}/{codigo}.pdf"
    subir(ruta, pdf)

    with engine.begin() as cx:
        cx.execute(
            text("update core.certificado_laboral set ruta_storage = :r where id = :id"),
            {"r": ruta, "id": certificado_id},
        )

    return {"ruta": ruta, "url": url_firmada(ruta), "codigo": codigo, "bytes": len(pdf)}


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------
try:
    from fastapi import APIRouter, HTTPException

    router = APIRouter(prefix="/documentos", tags=["documentos"])

    @router.post("/comprobante/{liquidacion_id}")
    def endpoint_comprobante(liquidacion_id: str):
        try:
            return generar_comprobante(liquidacion_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @router.post("/certificado/{certificado_id}")
    def endpoint_certificado(certificado_id: str, base_verificacion: str | None = None):
        try:
            url = f"{base_verificacion.rstrip('/')}/verificar/" if base_verificacion else None
            with engine.begin() as cx:
                cod = cx.execute(
                    text("select codigo_verificacion from core.certificado_laboral where id = :i"),
                    {"i": certificado_id},
                ).scalar_one_or_none()
            return generar_certificado(certificado_id, (url + cod) if url and cod else None)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @router.post("/comprobantes-periodo/{periodo_id}")
    def endpoint_periodo(periodo_id: str):
        """Genera el desprendible de todas las liquidaciones de un periodo."""
        with engine.begin() as cx:
            ids = [str(r[0]) for r in cx.execute(
                text("select id from core.liquidacion where periodo_id = :p and estado <> 'anulada'"),
                {"p": periodo_id},
            )]
        generados, errores = [], []
        for lid in ids:
            try:
                generados.append(generar_comprobante(lid))
            except Exception as e:  # noqa: BLE001
                errores.append({"liquidacion": lid, "error": str(e)})
        return {"generados": len(generados), "errores": errores}

except ImportError:  # el módulo también sirve como script suelto
    router = None


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3 or sys.argv[1] not in {"comprobante", "certificado"}:
        print("uso: python documentos.py [comprobante|certificado] <uuid>")
        raise SystemExit(1)
    fn = generar_comprobante if sys.argv[1] == "comprobante" else generar_certificado
    print(fn(sys.argv[2]))
