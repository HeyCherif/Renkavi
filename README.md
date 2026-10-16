# Renkavi

<img src="assets/brand/renkavi.png" width="96" alt="Logo Renkavi">

**Atelier local de traduction de documents et de récits illustrés.** Application Windows avec fenêtre native pywebview / Microsoft Edge WebView2.

Version **0.3.0-beta.2** — 16 octobre 2026. Cette version bêta ajoute un suivi global des traitements, suspend les lots en cas d’erreur mémoire ou de moteur indisponible, et limite les allocations OCR. Une relecture des traductions reste nécessaire.

## Installer la version Windows

1. Télécharger `Renkavi-0.3.0-beta.2-windows-x64.zip` dans les [releases](https://github.com/zamakz641-byte/Renkavi/releases).
2. Extraire entièrement l'archive et ouvrir `Renkavi/Renkavi.exe`.
3. Installer [Microsoft Edge WebView2 Runtime Evergreen x64](https://developer.microsoft.com/microsoft-edge/webview2/) s'il manque.
4. Configurer les modèles dans **Modèles** : les deux poids OCR, un traducteur et un moteur de restauration. Aucun poids IA n'est inclus dans le téléchargement de l'application.

Les exécutables ne sont pas signés numériquement. Windows peut demander de confirmer leur origine. Les signatures SHA-256 des archives figurent dans `SHA256SUMS`.

## Fonctions

- Projets contenant plusieurs fichiers et traitement batch avec pause/reprise.
- PDF texte ou scanné, DOCX, PNG/JPEG/WebP/BMP, CBZ/ZIP et dossiers d'images.
- OCR, traduction, restauration et composition ; correction des zones et des textes dans l'atelier.
- Fiche de contexte et glossaire persistants, dialogues voisins et références sources transmis au traducteur.
- Export au format d'origine et ZIP de projet, avec vérification des résultats manquants ou périmés.
- Catalogue avec téléchargements vérifiés par SHA-256, progression et reprise ; connecteurs compatibles OpenAI.
- Interface française et anglaise, fonctionnement local. Les connecteurs distants contactent le service choisi par l'utilisateur.

## Modèles et prérequis

L'OCR fonctionne avec RapidOCR / ONNX Runtime. Les modèles GGUF utilisent une installation séparée de [LM Studio](https://lmstudio.ai/) et sa CLI `lms`. Le catalogue propose plusieurs tailles et quantifications. Installer un modèle ne change pas la sélection active : choisir ensuite le modèle dans la bibliothèque.

LaMa ONNX, MI-GAN et OpenCV Telea fonctionnent sans environnement PyTorch. Le moteur Anime-Manga LaMa CUDA est optionnel et nécessite un Python séparé avec PyTorch CUDA ; indiquer son exécutable dans `RENKAVI_CUDA_PYTHON`. Les modèles et ces outils externes conservent leurs propres licences.

Les performances dépendent du modèle, de la mémoire disponible, de la quantité de texte et de la taille des pages. La première lecture OCR prépare le contexte du document ; une relance valide réutilise les résultats avant de charger les moteurs.

## Données et diagnostics

La distribution Windows conserve ses données inscriptibles sous `%LOCALAPPDATA%\Renkavi`. Une bibliothèque historique existante est réutilisée. Le code source conserve par défaut sa bibliothèque dans `data/` près du projet. La fenêtre ferme son serveur HTTP local ; un traitement actif est mis en pause à une limite de page avant la fermeture.

Les journaux de démarrage se trouvent dans `logs/startup.log` sous la bibliothèque choisie. Les projets, les originaux et les choix de modèles ne sont pas distribués dans les archives de release.

```powershell
Renkavi.exe --data-dir D:\MesDocumentsRenkavi --models-dir D:\MesModelesRenkavi
Renkavi.exe --browser
```

## Lancer depuis les sources

Python 3.14 x64 est utilisé pour la release Windows.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.venv\Scripts\python.exe run.py
```

`INSTALLER.bat` installe les dépendances ; `LANCER.bat` lance l'application. Le mode `--browser` et le mode `--no-browser` restent disponibles pour le diagnostic. Le port local est choisi automatiquement.

## Développer et construire

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -q
.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.venv\Scripts\python.exe scripts/build_release.py --version 0.3.0-beta.2
```

Voir [la procédure de release](docs/release.md). Le paquet portable est construit et testé sur Windows ; l'archive source est limitée aux fichiers applicatifs et aux tests synthétiques.

## Licences

Voir [LICENSE](LICENSE) pour le code original et [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) pour les composants tiers. Le dépôt public ne contient aucun document utilisateur ni poids de modèle.
