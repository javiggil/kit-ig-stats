"""
Servidor MCP de stats de Instagram para Claude Desktop.

Lee el token de pagina indefinido y el IG ID desde discovery.json
(en la misma carpeta que este archivo). Expone herramientas para
consultar y analizar el rendimiento del contenido.

Extrae todas las metricas disponibles por tipo de contenido. El refresh
parcial hace merge: actualiza los recientes y conserva el historico.

Instalacion:
    pip install "mcp[cli]"
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

from mcp.server.fastmcp import FastMCP

API_VERSION = "v22.0"
BASE = f"https://graph.facebook.com/{API_VERSION}"

HERE = os.path.dirname(os.path.abspath(__file__))
DISCOVERY_PATH = os.path.join(HERE, "discovery.json")
CACHE_PATH = os.path.join(HERE, "ig_cache.jsonl")

# Metricas "nucleo": se piden en un solo lote (rapido). Formato estandar.
CORE_METRICS = {
    "REELS": ["reach", "likes", "comments", "saved", "shares",
              "total_interactions", "views",
              "ig_reels_avg_watch_time", "ig_reels_video_view_total_time"],
    "FEED": ["reach", "likes", "comments", "saved", "shares",
             "total_interactions", "views"],
    "CAROUSEL_ALBUM": ["reach", "likes", "comments", "saved", "shares",
                       "total_interactions", "views"],
    "STORY": ["reach", "replies", "shares", "total_interactions"],
}

# Metricas "extra": mas nuevas, se piden una a una con metric_type=total_value.
# Si una no aplica al contenido, la API da error, se salta y queda apuntada en
# _EXTRA_RECHAZADAS (el refresh lo dice). Probado de v22 a v26 el 2026-10-21:
# follows, profile_visits, profile_activity y clips_replays_count NO existen
# para REELS (error #100), por eso ya no se piden. Retencion, % de no seguidores
# por reel y fuentes de visualizacion tampoco: solo estan en la app.
EXTRA_METRICS = {
    "REELS": ["reposts", "reels_skip_rate"],
    "FEED": ["profile_activity", "profile_visits", "follows", "reposts"],
    "CAROUSEL_ALBUM": ["profile_activity", "profile_visits", "follows", "reposts"],
    "STORY": ["navigation", "profile_activity", "profile_visits", "follows"],
}

BASE_FIELDS = ("id", "media_product_type", "media_type", "timestamp",
               "permalink", "caption_short")

# Cache de la cuenta: un dia por linea, seguidores/no seguidores x tipo.
ACCOUNT_CACHE_PATH = os.path.join(HERE, "ig_account_cache.jsonl")
# La API corta los dias a medianoche del Pacifico (end_time 07:00 UTC).
API_TZ = "America/Los_Angeles"

_EXTRA_RECHAZADAS = {}  # metrica -> ultimo error de la API en esta corrida

mcp = FastMCP("instagram-stats")


def _load_account():
    with open(DISCOVERY_PATH, encoding="utf-8") as f:
        disc = json.load(f)
    return disc["ig_accounts"][0]


def _fb_get(path, params=None, retries=3):
    acc = _load_account()
    if params is None:
        params = {}
    params["access_token"] = acc["page_access_token"]
    url = f"{BASE}/{path}?{urlencode(params)}"
    for attempt in range(retries + 1):
        try:
            with urlopen(url, timeout=30) as resp:
                return json.loads(resp.read().decode()), None
        except HTTPError as e:
            body = e.read().decode()
            try:
                err = json.loads(body)["error"]["message"]
            except Exception:
                err = body
            return None, err
        except (URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                time.sleep(3)
            else:
                return None, f"fallo de red: {e}"
    return None, "agotados reintentos"


def _extract_value(item):
    """Saca el valor de un item de insights en cualquiera de sus formatos."""
    if item.get("values"):
        return item["values"][0].get("value")
    if "total_value" in item:
        return item["total_value"].get("value")
    return None


def _get_insights(media_id, ptype):
    """Saca todas las metricas posibles para un media:
    nucleo en lote + extras una a una. Devuelve dict metrica->valor."""
    result = {}
    core = CORE_METRICS.get(ptype, CORE_METRICS["FEED"])

    # Lote nucleo
    data, err = _fb_get(f"{media_id}/insights", {"metric": ",".join(core)})
    if data and "data" in data:
        for item in data["data"]:
            v = _extract_value(item)
            if v is not None:
                result[item["name"]] = v
    else:
        # Si el lote falla, nucleo una a una
        for m in core:
            d, _ = _fb_get(f"{media_id}/insights", {"metric": m})
            if d and d.get("data"):
                v = _extract_value(d["data"][0])
                if v is not None:
                    result[m] = v
            time.sleep(0.05)

    # Extras una a una (con total_value)
    for m in EXTRA_METRICS.get(ptype, []):
        d, err = _fb_get(f"{media_id}/insights",
                         {"metric": m, "metric_type": "total_value"})
        if d and d.get("data"):
            v = _extract_value(d["data"][0])
            if v is not None:
                # profile_activity puede traer un dict con breakdowns; lo aplano
                if isinstance(v, dict):
                    result[m] = v.get("value", v)
                else:
                    result[m] = v
        elif err:
            _EXTRA_RECHAZADAS[m] = err[:160]
        time.sleep(0.05)

    return result


def _read_cache():
    rows = []
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def _write_cache(rows_by_id):
    rows = sorted(rows_by_id.values(),
                  key=lambda r: r.get("timestamp", ""), reverse=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return rows


def _parse_ts(ts):
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
    except Exception:
        return None


def _metric_keys(rows):
    keys = set()
    for r in rows:
        for k in r:
            if k not in BASE_FIELDS:
                keys.add(k)
    return sorted(keys)


@mcp.tool()
def account_overview() -> str:
    """Resumen en vivo de la cuenta de Instagram: usuario, seguidores y numero
    total de publicaciones. Consulta la API en tiempo real."""
    acc = _load_account()
    data, err = _fb_get(acc["ig_id"], {
        "fields": "username,name,followers_count,follows_count,media_count,biography"
    })
    if err:
        return f"Error consultando la cuenta: {err}"
    return (f"Cuenta: @{data.get('username')} ({data.get('name','')})\n"
            f"Seguidores: {data.get('followers_count')}\n"
            f"Siguiendo: {data.get('follows_count')}\n"
            f"Publicaciones: {data.get('media_count')}\n"
            f"Bio: {data.get('biography','')}")


def _row_from_media(m):
    """Construye la fila base de un media (sin insights todavia)."""
    ptype = m.get("media_product_type", "FEED")
    caption = (m.get("caption") or "").replace("\n", " ").strip()
    return ptype, {
        "id": m["id"],
        "media_product_type": ptype,
        "media_type": m.get("media_type"),
        "timestamp": m.get("timestamp"),
        "permalink": m.get("permalink"),
        "caption_short": caption[:120],
    }


def _process_media_chunked(media, chunk_size=15):
    """Procesa una lista de media en tandas. Pide insights de cada uno y
    GUARDA la cache despues de cada tanda (no de todo al final). Si un post
    falla por red en sus insights, se salta y la corrida sigue: nunca tira
    el trabajo ya hecho. Devuelve (n_procesados, n_saltados, set_metricas)."""
    by_id = {r["id"]: r for r in _read_cache()}
    found_metrics = set()
    processed = 0
    skipped = 0
    for i in range(0, len(media), chunk_size):
        chunk = media[i:i + chunk_size]
        for m in chunk:
            try:
                ptype, row = _row_from_media(m)
                insights = _get_insights(m["id"], ptype)
                row.update(insights)
                found_metrics.update(insights.keys())
                by_id[m["id"]] = row
                processed += 1
            except Exception:
                # un post que peta no rompe la corrida; se salta y sigue
                skipped += 1
            time.sleep(0.1)
        # persistir tras cada tanda: si la siguiente revienta, esto ya esta guardado
        _write_cache(by_id)
    return processed, skipped, found_metrics


def _list_media(limit=0, since=None, until=None):
    """Lista medias de la cuenta paginando del mas nuevo al mas viejo.
    limit>0 corta a los N mas recientes. since/until (datetime) acotan por
    fecha de publicacion: como la API devuelve de nuevo a viejo, en cuanto
    aparece un post mas antiguo que since se deja de paginar (corte temprano,
    no recorre todo el historial). Devuelve (lista_media, error)."""
    acc = _load_account()
    media = []
    params = {
        "fields": "id,caption,media_type,media_product_type,permalink,timestamp",
        "limit": 50,
    }
    path = f"{acc['ig_id']}/media"
    stop = False
    while True:
        data, err = _fb_get(path, params)
        if err:
            return None, err
        for m in data.get("data", []):
            ts = _parse_ts(m.get("timestamp", ""))
            if since and ts and ts < since:
                stop = True  # este y los siguientes son mas viejos: cortamos
                break
            if until and ts and ts > until:
                continue  # mas nuevo que el rango pedido: lo saltamos
            media.append(m)
        if stop:
            break
        if limit and len(media) >= limit:
            media = media[:limit]
            break
        paging = data.get("paging", {})
        after = paging.get("cursors", {}).get("after")
        if "next" in paging and after:
            params["after"] = after
            time.sleep(0.2)
        else:
            break
    return media, None


def _aviso_rechazadas():
    if not _EXTRA_RECHAZADAS:
        return ""
    return "\nMetricas extra rechazadas por la API: " + "; ".join(
        f"{m} ({e})" for m, e in sorted(_EXTRA_RECHAZADAS.items()))


@mcp.tool()
def refresh_data(limit: int = 0, chunk_size: int = 15) -> str:
    """Descarga las estadisticas del contenido desde la API de Instagram y
    actualiza la cache local con TODAS las metricas disponibles por tipo.

    limit=0 procesa todo el contenido. limit=N procesa solo los N mas recientes
    y CONSERVA el resto del historico (merge, no borra). Util para el dia a dia.

    A prueba de crash: procesa en tandas de chunk_size y GUARDA la cache despues
    de cada tanda. Si la corrida se corta a mitad (red, timeout), lo ya bajado
    queda guardado y se retoma sin empezar de cero. Un post que falla en sus
    insights se salta, no tira la corrida. Bajar todo el historial es seguro
    aunque tarde.

    Devuelve cuantas publicaciones se actualizaron, cuantas se saltaron y que
    metricas se obtuvieron."""
    media, err = _list_media(limit=limit)
    if err:
        return f"Error listando contenido: {err}"
    if not media:
        return "No se encontro contenido para procesar."
    processed, skipped, found = _process_media_chunked(media, chunk_size)
    total = len(_read_cache())
    scope = "todo el contenido" if not limit else f"los {len(media)} mas recientes"
    msg = (f"Cache actualizada ({scope}) en tandas de {chunk_size}. "
           f"Procesadas: {processed}. Total en cache: {total} publicaciones.\n"
           f"Metricas obtenidas: {', '.join(sorted(found)) if found else 'ninguna'}")
    if skipped:
        msg += f"\nSaltadas por fallo de red: {skipped} (vuelve a ejecutar para reintentarlas)."
    return msg + _aviso_rechazadas()


@mcp.tool()
def refresh_range(since: str, until: str = "", chunk_size: int = 15) -> str:
    """Descarga e inserta en cache SOLO las publicaciones de un rango de fechas,
    sin recorrer todo el historial. Pensado para el refresco semanal: pides la
    semana y no se baja nada mas, asi no tarda ni crashea.

    since: fecha de inicio inclusive, formato 'YYYY-MM-DD'.
    until: fecha de fin inclusive, formato 'YYYY-MM-DD'. Vacio = hasta hoy.

    Como la API lista del post mas nuevo al mas viejo, en cuanto aparece uno
    anterior a 'since' se deja de paginar (corte temprano) y solo se piden
    insights de los que caen en el rango. Misma proteccion en tandas y mismo
    merge con el historico que refresh_data. Ejemplo del domingo:
    refresh_range('2026-06-13', '2026-06-19')."""
    try:
        since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return "Formato de 'since' invalido. Usa 'YYYY-MM-DD', p. ej. 2026-06-13."
    if until:
        try:
            until_dt = datetime.strptime(until, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc)
        except ValueError:
            return "Formato de 'until' invalido. Usa 'YYYY-MM-DD', p. ej. 2026-06-19."
    else:
        until_dt = datetime.now(timezone.utc)
    if since_dt > until_dt:
        return "El rango esta invertido: 'since' es posterior a 'until'."

    media, err = _list_media(since=since_dt, until=until_dt)
    if err:
        return f"Error listando contenido: {err}"
    hasta = until if until else "hoy"
    if not media:
        return f"No hay publicaciones entre {since} y {hasta}."
    processed, skipped, found = _process_media_chunked(media, chunk_size)
    total = len(_read_cache())
    msg = (f"Cache actualizada con el rango {since} a {hasta}: {processed} "
           f"publicaciones. Total en cache: {total}.\n"
           f"Metricas obtenidas: {', '.join(sorted(found)) if found else 'ninguna'}")
    if skipped:
        msg += f"\nSaltadas por fallo de red: {skipped} (vuelve a ejecutar para reintentarlas)."
    return msg + _aviso_rechazadas()


@mcp.tool()
def list_available_metrics() -> str:
    """Lista que metricas hay guardadas en la cache actualmente."""
    rows = _read_cache()
    if not rows:
        return "La cache esta vacia. Ejecuta refresh_data primero."
    return "Metricas en cache: " + ", ".join(_metric_keys(rows))


def _read_account_cache():
    dias = {}
    if os.path.exists(ACCOUNT_CACHE_PATH):
        with open(ACCOUNT_CACHE_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    dias[r["dia"]] = r
                except Exception:
                    pass
    return dias


def _split_dia(acc, dia, tz):
    """views y reach de un dia (hora del Pacifico) por seguidor x tipo, mas los
    follows del dia. Nunca guarda 'paging': sus URLs llevan el token."""
    ini = datetime.strptime(dia, "%Y-%m-%d").replace(tzinfo=tz)
    # La API incluye el dia que EMPIEZA en 'until': se cierra un segundo antes
    # de la medianoche siguiente para que la fila no sume tambien el dia D+1.
    rango = {"since": int(ini.timestamp()),
             "until": int((ini + timedelta(days=1)).timestamp()) - 1}
    fila = {"dia": dia}
    data, err = _fb_get(f"{acc['ig_id']}/insights", {
        "metric": "views,reach", "period": "day", "metric_type": "total_value",
        "breakdown": "follow_type,media_product_type", **rango})
    if err:
        return None, err
    for item in data.get("data", []):
        tv = item.get("total_value", {})
        partes = {}
        for b in tv.get("breakdowns", []):
            for res in b.get("results", []):
                partes["_".join(res["dimension_values"]).lower()] = res.get("value")
        fila[item["name"]] = {"total": tv.get("value"), **partes}
    data, err = _fb_get(f"{acc['ig_id']}/insights", {
        "metric": "follows_and_unfollows", "period": "day",
        "metric_type": "total_value", "breakdown": "follow_type", **rango})
    if not err:
        for item in data.get("data", []):
            for b in item.get("total_value", {}).get("breakdowns", []):
                fila["follows_and_unfollows"] = {
                    res["dimension_values"][0].lower(): res.get("value")
                    for res in b.get("results", [])}
    data, err = _fb_get(f"{acc['ig_id']}/insights", {
        "metric": "follower_count", "period": "day", **rango})
    if not err:
        for item in data.get("data", []):
            vals = item.get("values") or []
            if vals:
                fila["follower_count"] = vals[-1].get("value")
    for m in ("views", "reach"):
        r = fila.get(m) or {}
        reel = (r.get("follower_reel") or 0) + (r.get("non_follower_reel") or 0)
        if reel:
            fila[f"{m}_reel_no_seg_pct"] = round(
                100 * (r.get("non_follower_reel") or 0) / reel, 1)
    return fila, None


@mcp.tool()
def account_follow_split(since: str, until: str = "") -> str:
    """Reparto seguidores / no seguidores de la CUENTA, dia a dia (dias de la
    API, hora del Pacifico): views y reach por follow_type x tipo de contenido
    (REEL, STORY...), mas follower_count y follows_and_unfollows del dia.
    Es lo unico de seguidores/no seguidores que da la API: por reel no existe.
    Solo vale como aproximacion de un reel en dias en que salio un unico reel.
    El reach por dia NO se suma entre dias (son cuentas unicas por dia).
    Guarda en ig_account_cache.jsonl y devuelve una tabla.

    since/until: 'YYYY-MM-DD' inclusive. until vacio = ayer."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(API_TZ)
    try:
        d0 = datetime.strptime(since, "%Y-%m-%d").date()
        d1 = (datetime.strptime(until, "%Y-%m-%d").date() if until
              else datetime.now(tz).date() - timedelta(days=1))
    except ValueError:
        return "Fechas en formato 'YYYY-MM-DD'."
    if d0 > d1:
        return "El rango esta invertido."
    acc = _load_account()
    cache = _read_account_cache()
    errores = []
    d = d0
    while d <= d1:
        fila, err = _split_dia(acc, d.isoformat(), tz)
        if err:
            errores.append(f"{d}: {err[:120]}")
        else:
            cache[fila["dia"]] = fila
        d += timedelta(days=1)
        time.sleep(0.1)
    with open(ACCOUNT_CACHE_PATH, "w", encoding="utf-8") as f:
        for k in sorted(cache):
            f.write(json.dumps(cache[k], ensure_ascii=False) + "\n")
    out = ["| Dia (PT) | Views reel | % no seg (views) | Reach reel | % no seg (reach) | follower_count |",
           "|---|---|---|---|---|---|"]
    for k in sorted(cache):
        if not (d0.isoformat() <= k <= d1.isoformat()):
            continue
        r = cache[k]
        vr = (r.get("views") or {})
        rr = (r.get("reach") or {})
        out.append(
            f"| {k} | {(vr.get('follower_reel') or 0) + (vr.get('non_follower_reel') or 0)} | "
            f"{r.get('views_reel_no_seg_pct')} | "
            f"{(rr.get('follower_reel') or 0) + (rr.get('non_follower_reel') or 0)} | "
            f"{r.get('reach_reel_no_seg_pct')} | {r.get('follower_count')} |")
    if errores:
        out.append("Errores: " + "; ".join(errores))
    return "\n".join(out)


@mcp.tool()
def top_content(metric: str = "views", limit: int = 10,
                content_type: str = "REELS") -> str:
    """Top de publicaciones de la cache ordenadas por una metrica.
    metric: cualquiera de las guardadas (views, reach, likes, comments, saved,
    shares, total_interactions, reposts, profile_visits, follows,
    ig_reels_avg_watch_time, reels_skip_rate, etc).
    content_type: REELS, FEED, CAROUSEL_ALBUM, STORY o ALL."""
    rows = _read_cache()
    if not rows:
        return "La cache esta vacia. Ejecuta refresh_data primero."
    if content_type != "ALL":
        rows = [r for r in rows if r.get("media_product_type") == content_type]
    rows = [r for r in rows if isinstance(r.get(metric), (int, float))]
    if not rows:
        return (f"Ninguna publicacion {content_type} tiene la metrica '{metric}'.\n"
                f"Metricas disponibles: {', '.join(_metric_keys(_read_cache()))}")
    rows.sort(key=lambda r: r.get(metric) or 0, reverse=True)
    out = [f"Top {min(limit,len(rows))} por {metric} ({content_type}):"]
    for i, r in enumerate(rows[:limit], 1):
        out.append(f"{i}. {r.get(metric):>10} {metric} | {r['timestamp'][:10]} | "
                   f"{r.get('caption_short','')[:55]}\n   {r.get('permalink','')}")
    return "\n".join(out)


@mcp.tool()
def performance_summary(days: int = 30, content_type: str = "REELS") -> str:
    """Resumen agregado del rendimiento en los ultimos N dias: totales y medias
    de todas las metricas numericas disponibles, y mejor publicacion del periodo."""
    rows = _read_cache()
    if not rows:
        return "La cache esta vacia. Ejecuta refresh_data primero."
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    sel = []
    for r in rows:
        if content_type != "ALL" and r.get("media_product_type") != content_type:
            continue
        ts = _parse_ts(r.get("timestamp", ""))
        if ts and ts >= cutoff:
            sel.append(r)
    if not sel:
        return f"No hay {content_type} en los ultimos {days} dias."

    n = len(sel)
    metrics = _metric_keys(sel)
    lines = [f"Ultimos {days} dias ({content_type}): {n} publicaciones"]
    for m in metrics:
        vals = [r[m] for r in sel if isinstance(r.get(m), (int, float))]
        if vals:
            tot = sum(vals)
            lines.append(f"  {m}: total {tot:,} | media {tot//len(vals):,}/post")
    best = max(sel, key=lambda r: r.get("views") or 0)
    lines.append(f"Mejor post: {best.get('views',0):,} views | {best['timestamp'][:10]} | "
                 f"{best.get('caption_short','')[:55]}")
    return "\n".join(lines)


@mcp.tool()
def search_captions(query: str, limit: int = 20) -> str:
    """Busca publicaciones cuyo texto (caption) contenga la consulta dada,
    ordenadas por views. Util para analizar como rinde un tema concreto."""
    rows = _read_cache()
    if not rows:
        return "La cache esta vacia. Ejecuta refresh_data primero."
    q = query.lower()
    hits = [r for r in rows if q in (r.get("caption_short", "") or "").lower()]
    if not hits:
        return f"Sin resultados para '{query}'."
    hits.sort(key=lambda r: r.get("views") or 0, reverse=True)
    out = [f"{len(hits)} resultados para '{query}' (por views):"]
    for r in hits[:limit]:
        out.append(f"  {r.get('views','-'):>8} views | {r['timestamp'][:10]} | "
                   f"{r.get('caption_short','')[:55]}")
    return "\n".join(out)


@mcp.tool()
def media_detail(media_id: str) -> str:
    """Detalle completo de una publicacion concreta por su id (todas las
    metricas guardadas en cache mas el enlace)."""
    for r in _read_cache():
        if r.get("id") == media_id:
            lines = [f"Publicacion {media_id} ({r.get('media_product_type')})",
                     f"Fecha: {r.get('timestamp')}",
                     f"Enlace: {r.get('permalink')}",
                     f"Texto: {r.get('caption_short')}", "Metricas:"]
            for k, v in r.items():
                if k not in BASE_FIELDS:
                    lines.append(f"  {k}: {v}")
            return "\n".join(lines)
    return f"No encuentro {media_id} en cache. Ejecuta refresh_data."


def _fetch_comments(media_id, max_comments=200):
    """Saca los comentarios de primer nivel de un media, paginando.
    Devuelve (lista_comentarios, error). Cada comentario: dict con
    text, username, timestamp, like_count. No baja a las respuestas (hilos)."""
    comments = []
    params = {
        "fields": "text,username,timestamp,like_count",
        "limit": 50,
    }
    path = f"{media_id}/comments"
    while True:
        data, err = _fb_get(path, params)
        if err:
            return None, err
        comments.extend(data.get("data", []))
        if len(comments) >= max_comments:
            comments = comments[:max_comments]
            break
        paging = data.get("paging", {})
        after = paging.get("cursors", {}).get("after")
        if "next" in paging and after:
            params["after"] = after
            time.sleep(0.15)
        else:
            break
    return comments, None


def _format_comments(comments):
    """Formatea una lista de comentarios ya ordenada para salida de texto."""
    out = []
    for c in comments:
        text = (c.get("text") or "").replace("\n", " ").strip()
        user = c.get("username", "?")
        ts = (c.get("timestamp", "") or "")[:10]
        likes = c.get("like_count", 0)
        out.append(f"  [{likes:>3}♥] @{user} ({ts}): {text}")
    return out


@mcp.tool()
def get_comments(media_id: str, max_comments: int = 200) -> str:
    """Lee el TEXTO de los comentarios de primer nivel de una publicacion
    concreta por su id, en vivo desde la API (no usa cache). Devuelve cada
    comentario con su texto, autor, fecha y numero de likes, ordenados por
    likes (los que mas resonaron primero). No incluye las respuestas a
    comentarios (hilos). Util para leer que dice la gente en un reel concreto
    y sacar temas u objeciones para contenido.

    El media_id se saca de top_content, performance_summary o media_detail
    (campo permalink/id de la cache)."""
    comments, err = _fetch_comments(media_id, max_comments)
    if err:
        return f"Error leyendo comentarios de {media_id}: {err}"
    if not comments:
        return f"La publicacion {media_id} no tiene comentarios (o no son accesibles)."
    comments.sort(key=lambda c: c.get("like_count", 0) or 0, reverse=True)
    header = f"{len(comments)} comentarios de {media_id} (por likes):"
    return header + "\n" + "\n".join(_format_comments(comments))


@mcp.tool()
def recent_comments(num_reels: int = 10, max_per_reel: int = 100,
                    content_type: str = "REELS") -> str:
    """Barrido: lee los comentarios de las ultimas N publicaciones (por defecto
    los 10 reels mas recientes que haya en cache) y los devuelve agrupados por
    publicacion, en vivo desde la API. Cada comentario con texto, autor, fecha
    y likes. No incluye hilos de respuestas. Util para revisar de golpe que ha
    llegado en los ultimos reels y detectar patrones, preguntas repetidas u
    objeciones que alimenten ideas de contenido.

    Requiere haber ejecutado refresh_data antes (saca de la cache que reels son
    los mas recientes y sus ids). num_reels alto = mas llamadas a la API y mas
    lento."""
    rows = _read_cache()
    if not rows:
        return "La cache esta vacia. Ejecuta refresh_data primero."
    if content_type != "ALL":
        rows = [r for r in rows if r.get("media_product_type") == content_type]
    rows = [r for r in rows if r.get("timestamp")]
    rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    sel = rows[:num_reels]
    if not sel:
        return f"No hay publicaciones {content_type} en cache."

    blocks = []
    total = 0
    for r in sel:
        mid = r.get("id")
        comments, err = _fetch_comments(mid, max_per_reel)
        date = (r.get("timestamp", "") or "")[:10]
        cap = (r.get("caption_short", "") or "")[:55]
        if err:
            blocks.append(f"--- {date} | {cap}\n  (error: {err})")
            continue
        if not comments:
            blocks.append(f"--- {date} | {cap}\n  (sin comentarios)")
            continue
        comments.sort(key=lambda c: c.get("like_count", 0) or 0, reverse=True)
        total += len(comments)
        blocks.append(f"--- {date} | {cap} | {len(comments)} comentarios\n"
                      + "\n".join(_format_comments(comments)))
        time.sleep(0.1)

    header = (f"Comentarios de los ultimos {len(sel)} {content_type} "
              f"({total} comentarios en total):")
    return header + "\n" + "\n".join(blocks)


if __name__ == "__main__":
    mcp.run()
