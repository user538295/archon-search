# Guide d'installation

## Prérequis

Avant de commencer l'installation, assurez-vous que votre système dispose des éléments suivants :

- Python 3.12 ou version supérieure
- pip ou uv pour la gestion des paquets
- Au moins 512 Mo d'espace disque disponible

## Installation de base

Pour installer le logiciel, exécutez la commande suivante dans votre terminal :

```bash
pip install archon-search
```

Ou avec uv :

```bash
uv add archon-search
```

## Configuration initiale

Après l'installation, créez un fichier de configuration dans votre répertoire personnel :

```bash
archon-search config init
```

Cette commande génère un fichier `archon-search.toml` avec les paramètres par défaut.

## Démarrage du serveur

Pour démarrer le serveur, utilisez la commande :

```bash
archon-search start
```

Le serveur écoute sur le port 8080 par défaut. Vous pouvez modifier ce paramètre dans le fichier de configuration.

## Vérification de l'installation

Pour vérifier que l'installation s'est déroulée correctement, exécutez :

```bash
archon-search status
```

Si le serveur fonctionne correctement, vous verrez un message indiquant que tous les composants sont opérationnels.
