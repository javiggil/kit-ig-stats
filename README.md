# ig-stats

Las estadísticas de tu Instagram dentro de Claude. Le preguntas «¿qué reels del último mes se han compartido más?» o «¿qué me están preguntando en los comentarios?» y lo saca de la API oficial de Instagram.

Es un servidor MCP: un pequeño programa que Claude arranca y que le da herramientas nuevas.

## Qué le puedes pedir

| Herramienta | Para qué |
|---|---|
| `refresh_data` / `refresh_range` | Descarga tus publicaciones y sus métricas (vistas, alcance, compartidos, guardados, likes, comentarios, tiempo de visionado...). Hazlo primero. |
| `top_content` | Lo mejor por la métrica que digas: «top 10 por compartidos». |
| `performance_summary` | Medias y totales de los últimos N días. |
| `account_overview` | Seguidores, publicaciones y bio, en vivo. |
| `account_follow_split` | Por día, cuánto viene de seguidores y cuánto de gente que no te sigue. |
| `search_captions` | Busca en tus textos: «¿qué reels hablé de precios?». |
| `media_detail` | Todo de una publicación. |
| `get_comments` / `recent_comments` | Lee los comentarios de un reel o de los últimos diez: ideas de contenido y objeciones repetidas. |
| `list_available_metrics` | Qué métricas hay en tus datos. |

Los datos se quedan en tu ordenador (`ig_cache.jsonl`).

## Requisitos

- Una cuenta de Instagram **profesional** (de empresa o de creador) unida a una **página de Facebook**.
- Python 3.10 o superior.
- Claude Code o Claude Desktop.

## Instalación

### 1. Bajar e instalar

```bash
git clone https://github.com/javiggil/kit-ig-stats.git
cd kit-ig-stats
python3 -m venv .venv
.venv/bin/pip install "mcp[cli]"
```

### 2. Crear la app de Meta y sacar un token (una vez, unos 15 minutos)

1. Entra en [developers.facebook.com](https://developers.facebook.com/), crea una cuenta de desarrollador si no la tienes y pulsa **Crear app**. Tipo: **Empresa** (u «Otro» y luego «Empresa»).
2. En la app, añade el producto **Instagram** con la opción de **inicio de sesión con Facebook** (Instagram API with Facebook Login).
3. En **Configuración de la app > Básica** copia el **identificador de la app** y la **clave secreta**.
4. Abre el [Explorador de la API Graph](https://developers.facebook.com/tools/explorer/), elige tu app arriba a la derecha y en **Permisos** añade:
   `pages_show_list`, `pages_read_engagement`, `instagram_basic`, `instagram_manage_insights`, `instagram_manage_comments`, `business_management`.
5. Pulsa **Generate Access Token**, acepta con tu cuenta y elige tu página y tu Instagram. Copia el token: dura una hora.

### 3. Configurar

```bash
python3 configurar.py
```

Te pide el identificador, la clave secreta y el token. Los cambia por un token que no caduca y deja `discovery.json` en la carpeta. **Ese fichero es tu llave: no lo subas a ningún sitio** (ya está en `.gitignore`).

### 4. Conectarlo a Claude

**Claude Code** (desde la carpeta de ig-stats):

```bash
claude mcp add instagram-stats --scope user -- "$(pwd)/.venv/bin/python" "$(pwd)/ig_mcp_server.py"
```

**Claude Desktop**: en Configuración > Desarrollador > Editar configuración, añade dentro de `mcpServers`:

```json
"instagram-stats": {
  "command": "/ruta/a/ig-stats/.venv/bin/python",
  "args": ["/ruta/a/ig-stats/ig_mcp_server.py"]
}
```

Reinicia Claude y pídele: «Actualiza mis datos de Instagram y dime mis 5 reels más compartidos».

## Si algo falla

- **«Error validating access token»:** el token de página se invalida si cambias la contraseña de Facebook o quitas permisos a la app. Vuelve a los pasos 2.4 y 3.
- **Una métrica sale vacía:** Instagram no da todas las métricas para todos los tipos de publicación; el servidor lo dice al actualizar.
- **«No tiene cuenta de Instagram profesional unida»:** en Instagram, Configuración > Tipo de cuenta, pásala a profesional y únela a tu página.

Lo que la API no da: retención segundo a segundo y de dónde vienen las visualizaciones. Eso solo está en la app de Instagram.
