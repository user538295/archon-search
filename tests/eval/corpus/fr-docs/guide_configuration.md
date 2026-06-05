# Guide de configuration

## Fichier de configuration principal

Le fichier de configuration `archon-search.toml` contrôle tous les aspects du comportement du serveur.

### Section [server]

```toml
[server]
host = "0.0.0.0"
port = 8080
workers = 4
```

### Section [database]

```toml
[database]
path = "~/.archon-search/data"
embedding_model = "BAAI/bge-small-en-v1.5"
multilingual = false
language_detection_confidence_threshold = 0.7
```

### Section [search]

```toml
[search]
top_k_retrieve = 40
top_k_return = 10
reranker_model = "cross-encoder/ms-marco-MiniLM-L-6-v2"
```

## Configuration multilingue

Pour activer la détection automatique de langue, configurez les paramètres suivants :

```toml
[database]
multilingual = true
language_detection_confidence_threshold = 0.75
```

Le seuil de confiance détermine quand une détection est considérée fiable. En dessous du seuil, le document reçoit la valeur `"unknown"`.

## Variables d'environnement

Les variables d'environnement remplacent les valeurs du fichier de configuration :

- `ARCHON_SEARCH_API_KEY` : Clé API pour l'authentification
- `ARCHON_SEARCH_HOST` : Adresse d'écoute du serveur
- `ARCHON_SEARCH_PORT` : Port d'écoute du serveur
