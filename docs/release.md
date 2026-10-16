# Releases Windows

La version `0.3.0-beta.1` est une **préversion** : les fonctions évoluent et une
sauvegarde des projets est recommandée avant une mise à jour.

## Utiliser la version portable

1. Télécharger `Renkavi-0.3.0-beta.1-windows-x64.zip` depuis les releases GitHub.
2. Extraire entièrement le ZIP dans un dossier accessible en écriture.
3. Lancer `Renkavi/Renkavi.exe`.

Windows x64 et le runtime Microsoft Edge WebView2 sont requis. Windows peut
afficher un avertissement SmartScreen pour cet exécutable non signé.
Le dossier `_internal` fait partie de l'application et doit rester près de
`Renkavi.exe`. Python n'est pas requis pour la version portable.

Les projets, paramètres et modèles sont enregistrés sous `%LOCALAPPDATA%\Renkavi`
par défaut. L'application ne contient aucun modèle, projet personnel ou compte
préconfiguré. Le gestionnaire de modèles permet d'installer les poids séparément.
Un worker PyTorch/CUDA peut être configuré avec `RENKAVI_CUDA_PYTHON` vers un Python
externe compatible ; PyTorch et CUDA ne sont pas inclus dans le ZIP.

Pour utiliser le navigateur système : `Renkavi.exe --browser`.
Les options `--data-dir`, `--models-dir` et `--port` permettent de personnaliser
l'installation. `--no-browser` lance uniquement le serveur local.

## Construire

Utiliser Windows x64 et Python 3.14 dans un environnement virtuel dédié :

```powershell
python -m venv .venv-build
.venv-build\Scripts\python -m pip install -r requirements-build.txt
.venv-build\Scripts\python -m unittest discover -s tests -v
.venv-build\Scripts\python scripts/build_release.py --version 0.3.0-beta.1
```

Le script construit une application PyInstaller en dossier, exécute le programme
figé avec `--no-browser --smoke-test 2` dans une bibliothèque temporaire, puis
produit dans `release/` :

- `Renkavi-<version>-windows-x64.zip` : application portable.
- `Renkavi-<version>-source.zip` : sources sélectionnées par une liste explicite.
- `SHA256SUMS` : sommes SHA-256 des deux archives.

Le test de démarrage vérifie le serveur et les ressources statiques ; la fenêtre
WebView2 et les traitements OCR/traduction doivent également être essayés sur une
machine Windows avant de qualifier une version de stable. Le paquet n'est pas
signé et sa reproductibilité binaire n'est pas garantie.

Le manifeste du paquet inclut les fichiers frontend, les polices et leurs licences,
les ressources de marque et les sources backend nécessaires au worker externe.
Les répertoires de données, poids, archives historiques et benchmarks sont exclus.
Les licences et métadonnées des dépendances installées sont conservées dans
`_internal/licenses/`, avec `inventory.json`. Construire dans un environnement
dédié évite d'y ajouter des paquets étrangers à l'application.

Pour inspecter uniquement les sources sélectionnées, sans construire l'exécutable :

```powershell
python scripts/build_release.py --source-only --version 0.3.0-beta.1
```

Vérification d'une archive téléchargée :

```powershell
Get-FileHash .\Renkavi-0.3.0-beta.1-windows-x64.zip -Algorithm SHA256
```

Comparer la valeur obtenue à la ligne correspondante de `SHA256SUMS`.

## GitHub Actions

Le workflow `Windows release` teste et construit sur un runner Windows. Un
lancement manuel conserve les archives comme artefacts de CI. Un tag `v*`
publie les archives et leurs sommes en **prerelease**, après réussite des tests
et du démarrage de l'exécutable. Seul le job de publication dispose du droit
`contents: write`. La publication d'une version stable reste une décision humaine.
