# WFM Optimizer API

Backend que ejecuta el optimizador de turnos y escribe en Supabase.

## Variables de entorno (configurar en Render)
- DB_URL: cadena de conexión del pooler de Supabase (postgresql://...)
- API_KEY: clave secreta para proteger el endpoint

## Endpoint
POST /generar-roster (multipart/form-data)
- Header: X-API-Key
- Campos: mes, campana, archivo (CSV), aht, sla, asa, occ, utl, esp_max, largo, nda_obj, paciencia, estructura
