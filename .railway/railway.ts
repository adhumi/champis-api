// Infrastructure as Code Railway : les trois services du projet « champis » (spec › Backend ›
// déploiement, décision du 4 nov. 2026). Config as Code (railway.toml) est déprécié et n'est plus lu
// après le 1er décembre 2026 ; les services cron créés après le 28 août 2026 ne le lisent déjà plus.
//
// Appliqué par la CLI depuis un poste (jamais lu par Railway au déploiement) :
//   railway login && railway link            # projet champis, environnement production
//   railway config plan                       # montre les changements, sans rien appliquer
//   railway config apply                      # applique après confirmation
// Première fois : `railway config migrate --apply` traduit railway.toml ici et vide le réglage
// « Config file path » du service champis ; comparer avec ce fichier, garder celui-ci.
//
// Le cron Railway est en UTC : 2 h et 14 h font 3 h et 15 h à Paris en hiver, une heure de plus en
// été ; on garde l'UTC fixe. Chaque exécution doit se terminer (≈ 40 min pour la France) ; si la
// précédente tourne encore, Railway saute l'exécution. Un volume ne se monte que sur un seul service
// (doc Railway) : les crons n'en ont pas et relaient le cache météo par le bucket (--pull/--publish).
import { bucket, defineRailway, github, postgres, preserve, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const source = github("adhumi/champis", { branch: "main" }); // Dockerfile à la racine, détecté par Railway

  const db = postgres("Postgres");
  const storage = bucket("storage", { region: "ams" }); // PMTiles, amorçage météo, instantané du cache
  const data = volume("champis-volume", { region: "europe-west4-drams3a", sizeMB: 50000 });

  // Identifiants du bucket et clés : posés une fois sur l'API (« Connect Service to Bucket »),
  // référencés par les crons ; `preserve()` garde les valeurs déjà dans Railway, jamais dans le dépôt.
  const api = service("champis", {
    source,
    healthcheck: "/health",
    healthcheckTimeout: 120,
    deploy: { restartPolicyType: "on_failure", restartPolicyMaxRetries: 5 },
    replicas: { "europe-west4-drams3a": 1 },
    volumeMounts: { "/data": data },
    env: {
      AWS_ACCESS_KEY_ID: preserve(),
      AWS_SECRET_ACCESS_KEY: preserve(),
      AWS_ENDPOINT_URL: preserve(),
      AWS_DEFAULT_REGION: preserve(),
      AWS_S3_BUCKET_NAME: preserve(),
      CHAMPIS_OPEN_METEO_API_KEY: preserve(),
      CHAMPIS_TILES_MODE: "proxy",
      DATABASE_URL: db.env.DATABASE_URL,
      // CHAMPIS_WARM_HOURS et CHAMPIS_NOTIFY_HOURS restent vides (valeur de l'image) : les crons
      // ci-dessous font le travail ; CHAMPIS_WORKERS garde le défaut de l'image (2).
    },
  });

  const bucketEnv = {
    AWS_ACCESS_KEY_ID: api.env.AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY: api.env.AWS_SECRET_ACCESS_KEY,
    AWS_ENDPOINT_URL: api.env.AWS_ENDPOINT_URL,
    AWS_DEFAULT_REGION: api.env.AWS_DEFAULT_REGION,
    AWS_S3_BUCKET_NAME: api.env.AWS_S3_BUCKET_NAME,
    CHAMPIS_OPEN_METEO_API_KEY: api.env.CHAMPIS_OPEN_METEO_API_KEY,
  };

  // Pré-chauffe : repart de l'instantané publié, calcule la France, redépose l'instantané.
  const warm = service("champis-warm", {
    source,
    start: "uv run champis-api -q warm --pull --publish",
    deploy: { cronSchedule: "0 2,14 * * *", restartPolicyType: "never" },
    replicas: { "europe-west4-drams3a": 1 },
    env: bucketEnv,
  });

  // Notification « mes coins », après la pré-chauffe du matin.
  const notify = service("champis-notify", {
    source,
    start: "uv run champis-api -q notify --pull",
    deploy: { cronSchedule: "0 6 * * *", restartPolicyType: "never" },
    replicas: { "europe-west4-drams3a": 1 },
    env: {
      ...bucketEnv,
      DATABASE_URL: db.env.DATABASE_URL,
      CHAMPIS_APNS_KEY: preserve(),
      CHAMPIS_APNS_KEY_ID: preserve(),
      CHAMPIS_APNS_TEAM_ID: preserve(),
      CHAMPIS_APNS_TOPIC: "fr.adhumi.champis",
    },
  });

  return project("champis", { resources: [api, warm, notify, db, storage, data] });
});
