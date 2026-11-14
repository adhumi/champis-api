"""En-têtes de sécurité de toutes les réponses du backoffice (14 nov. 2026).

Middleware ASGI pur, posé à l'extérieur des autres : il voit aussi les réponses du limiteur (429,
413) et les 404 d'un backoffice fermé. Sur tout chemin `/admin` :

- `Content-Security-Policy` : rien par défaut, scripts, styles, polices, images et requêtes du
  même hôte seulement (`data:` pour les images : Tabler dessine ses icônes de formulaire en SVG
  dans sa feuille de style). Ni script ni style en ligne : les gabarits n'en ont pas (les barres
  sont des SVG ou des `<progress>`, la règle `noscript` est un fichier), htmx n'injecte pas sa
  feuille d'indicateurs et n'évalue rien (`htmx-config` dans `base.html`). Formulaires vers le
  même hôte, pas de `<base>`, aucun cadre ;
- `X-Frame-Options: DENY` (les navigateurs sans `frame-ancestors`) ;
- `X-Content-Type-Options: nosniff` ;
- `Referrer-Policy: same-origin` : une adresse du backoffice ne part jamais vers un autre site (le
  bucket, GitHub) quand on suit un lien. Pas `no-referrer` : vérifié dans Chromium, un formulaire
  envoyé depuis une page sous `no-referrer` porte `Origin: null`, que `auth.same_origin` refuse
  (403 à la connexion et à chaque action) ; accepter `null` ouvrirait ce contrôle à n'importe
  quelle page tierce ;
- `Strict-Transport-Security` quand la requête est arrivée en HTTPS (TLS est terminé par Railway :
  `X-Forwarded-Proto`).

Un test rend chaque écran et vérifie qu'aucun gabarit ne réintroduit de script ou de style en
ligne (`test_admin_security.py`).
"""

from __future__ import annotations

PREFIX = "/admin"
CSP = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    )
)
HEADERS = {
    "content-security-policy": CSP,
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}
HSTS = ("strict-transport-security", "max-age=31536000; includeSubDomains")


class AdminSecurityHeaders:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path != PREFIX and not path.startswith(PREFIX + "/"):
            await self.app(scope, receive, send)
            return
        extra = dict(HEADERS)
        forwarded = [v for k, v in scope.get("headers", []) if k.lower() == b"x-forwarded-proto"]
        proto = forwarded[-1].decode("latin-1") if forwarded else scope.get("scheme", "http")
        if proto.strip().lower() == "https":
            extra[HSTS[0]] = HSTS[1]

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                kept = [
                    (k, v) for k, v in message.get("headers", []) if k.lower().decode() not in extra
                ]
                added = [(k.encode(), v.encode()) for k, v in extra.items()]
                message = {**message, "headers": kept + added}
            await send(message)

        await self.app(scope, receive, send_with_headers)
