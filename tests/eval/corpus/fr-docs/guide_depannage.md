# Guide de dépannage

## Problèmes courants

### Le serveur ne démarre pas

**Symptôme** : La commande `archon-search start` échoue avec une erreur.

**Solutions** :

1. Vérifiez que le port 8080 n'est pas déjà utilisé :
   ```bash
   lsof -i :8080
   ```

2. Vérifiez les permissions du répertoire de données :
   ```bash
   ls -la ~/.archon-search/
   ```

3. Consultez les journaux pour plus de détails :
   ```bash
   archon-search status --verbose
   ```

### Erreurs d'authentification

**Symptôme** : Les requêtes renvoient `401 Unauthorized`.

**Solutions** :

1. Vérifiez que le jeton API est correct
2. Régénérez le jeton si nécessaire :
   ```bash
   archon-search key rotate
   ```

### Résultats de recherche de mauvaise qualité

**Symptôme** : Les recherches ne retournent pas les documents attendus.

**Solutions** :

1. Réindexez la collection concernée
2. Vérifiez la configuration du modèle d'embeddings
3. Augmentez la valeur de `top_k_retrieve` pour récupérer plus de candidats avant le reclassement

### Problèmes de détection de langue

**Symptôme** : Les documents multilingues reçoivent la valeur `"unknown"`.

**Solutions** :

1. Vérifiez que le modèle FastText est installé :
   ```bash
   archon-search install --multilingual
   ```

2. Réduisez le seuil de confiance dans la configuration si nécessaire
