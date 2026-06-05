# Référence de l'API REST

## Authentification

Toutes les requêtes nécessitent un jeton d'authentification Bearer. Incluez l'en-tête suivant dans chaque requête :

```
Authorization: Bearer <votre_jeton>
```

## Endpoints principaux

### POST /search

Effectue une recherche dans une collection.

**Corps de la requête :**

```json
{
  "collection": "ma_collection",
  "query": "votre requête de recherche",
  "top_k": 5,
  "filters": {
    "language": "fr"
  }
}
```

**Réponse :**

```json
{
  "results": [
    {
      "doc_id": "doc-001",
      "score": 0.95,
      "text": "Extrait du document pertinent",
      "language": "fr"
    }
  ]
}
```

### GET /status

Retourne l'état actuel du serveur et des collections disponibles.

### POST /ingest

Ingère un document dans une collection spécifiée. Supporte les formats Markdown, texte brut et PDF.

## Codes d'erreur

- `400 Bad Request` : Paramètres de requête invalides
- `401 Unauthorized` : Jeton manquant ou invalide
- `404 Not Found` : Collection introuvable
- `422 Unprocessable Entity` : Erreur de validation des données
- `429 Too Many Requests` : Limite de débit atteinte
- `500 Internal Server Error` : Erreur interne du serveur
