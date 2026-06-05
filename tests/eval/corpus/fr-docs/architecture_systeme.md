# Architecture du système

## Vue d'ensemble

Le système est conçu selon une architecture en couches qui sépare clairement les responsabilités :

1. **Couche de présentation** : API REST et interface MCP
2. **Couche métier** : Pipeline de recherche et logique d'indexation
3. **Couche de données** : Magasin vectoriel LanceDB et index FTS

## Pipeline d'ingestion

Lors de l'ingestion d'un document, le système effectue les étapes suivantes :

1. **Analyse** : Le document est analysé et converti en texte brut (Markdown)
2. **Détection de langue** : La langue dominante est identifiée via FastText
3. **Découpage** : Le texte est découpé en segments (chunks) de taille configurable
4. **Vectorisation** : Chaque segment est converti en vecteur dense via fastembed
5. **Indexation** : Les vecteurs sont stockés dans LanceDB avec l'index HNSW

## Pipeline de recherche

La recherche hybride combine deux méthodes complémentaires :

- **Recherche vectorielle dense** : Basée sur la similarité cosinus entre vecteurs d'embeddings
- **Recherche plein texte (FTS)** : Basée sur BM25 avec tokenisation adaptée à la langue

Les résultats sont fusionnés via RRF (Reciprocal Rank Fusion) puis reclassés par un modèle cross-encoder.

## Routage multi-collection

Le routeur multi-collection sélectionne automatiquement la ou les collections les plus pertinentes pour une requête donnée, en utilisant des centroïdes de collection pré-calculés.
