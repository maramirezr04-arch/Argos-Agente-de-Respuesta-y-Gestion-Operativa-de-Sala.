# 🎤 Guion de presentación — Argos

> Estructura sugerida: **8 diapositivas**, ~10–12 min + demo en vivo.
> Idea central a repetir: *"Argos vigila lo que antes nadie alcanzaba a vigilar, y avisa solo."*

---

## Slide 1 — Portada
**Mostrar:** logo 🤖 Argos + "Monitoreo automático de remisiones — Tienda 456 Parque Antenas".

**Decir:** "Les voy a presentar Argos, un asistente que automatiza el seguimiento de remisiones para que ningún cliente se quede esperando su mercancía."

---

## Slide 2 — El problema (engancha con dolor real)
**Mostrar:** lista corta de "Antes".
- Revisar el OMS a mano cada cierto tiempo.
- Depende de que alguien esté disponible y se acuerde.
- Los pedidos atrasados se detectan tarde.
- No había forma de medir el tiempo de respuesta del equipo.

**Decir:** "El reto no era falta de ganas, era que una persona no puede estar revisando el sistema cada 15 minutos toda la jornada."

---

## Slide 3 — La solución en una frase
**Mostrar:** el one-pager (`one_pager.html`) o la frase grande.

**Decir:** "Argos descarga la información cada ~15 minutos, detecta lo atrasado y le avisa solo a la persona correcta por Google Chat. Cero revisión manual."

---

## Slide 4 — Cómo funciona (diagrama)
**Mostrar:** `arquitectura.svg`.

**Decir:** recorre el flujo de izquierda a derecha:
"Toma datos del OMS y XD → los descarga con 3 navegadores en paralelo → los ordena en Google Sheets → aplica la lógica (detecta vencidas, calcula tiempos, prioriza C&C) → y manda los mensajes a 4 espacios de Google Chat."

---

## Slide 5 — Lo que reciben (los 4 mensajes)
**Mostrar:** capturas reales de cada mensaje (jefes grupal, jefe individual, vendedor, KPI).

**Decir, por cada uno:**
- **Jefes/piso:** semáforo 🟢🟡🔴 y alerta 🚨 de prioridad (C&C / XD Expreso).
- **Jefe individual:** su desglose por vendedor.
- **Vendedor:** sus remisiones una por una con número, SKU y cuánto llevan desde la asignación en tienda.
- **Tiempos/KPI:** ranking de productividad al cierre.

---

## Slide 6 — DEMO EN VIVO ⭐
**Hacer:** correr `demo.py` (navegador visible, webhooks redirigidos a un espacio de prueba).

**Decir:** "Esto es Argos trabajando en tiempo real —descarga, procesa y manda el mensaje— sin tocar nada."

> *Tip: ten el demo ya abierto y probado antes; es el momento que más convence.*

---

## Slide 7 — Por qué es confiable
**Mostrar:** los "pills" del one-pager.
- 🔄 Se actualiza solo desde GitHub (no hay que reinstalar).
- 🐶 Watchdog que lo revive si se cae.
- 🔁 Reintentos automáticos ante fallos de red.
- 💾 Respaldo local + 🖥️ dashboard de control web.
- 🩺 Reporte de salud diario al cierre.

**Decir:** "No es un script frágil: se mantiene solo y avisa de su propio estado."

---

## Slide 8 — Resultados y futuro
**Mostrar:** números de impacto (llénalos con tus datos reales o estimados).
- Ciclos de monitoreo al día: ~**[N]**
- Remisiones vigiladas: **[N]**
- Reducción de vencidas desde la implementación: **[%]**
- Tiempo de revisión manual ahorrado: **[horas/día]**

**Cierre:** "Argos hoy cubre la tienda completa; el siguiente paso puede ser [escalarlo a otra tienda / nuevos KPIs / etc.]."

---

## 📌 Checklist antes de presentar
- [ ] Demo probado y funcionando (`demo.py`).
- [ ] Capturas de los 4 mensajes listas.
- [ ] Números de impacto del Slide 8 llenados con datos reales.
- [ ] Dashboard abierto en una pestaña por si preguntan "¿y cómo se controla?".
- [ ] El bot corriendo en producción para enseñar mensajes reales si hace falta.

## ❓ Preguntas probables y respuestas
- *"¿Y si se cae la computadora?"* → Watchdog lo reinicia + respaldo local; el dashboard muestra el estado de cada PC.
- *"¿Quién lo mantiene?"* → Se actualiza solo desde GitHub; los ajustes se hacen desde el dashboard, sin tocar código.
- *"¿Manda spam?"* → No: solo avisa a quien tiene pendientes; sin pendientes, no hay mensaje.
- *"¿Cómo sé que sigue vivo?"* → Reporte de salud diario al cierre (ciclos, remisiones, incidencias).
