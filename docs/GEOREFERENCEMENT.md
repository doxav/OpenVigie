# Géoréférencement : préparer un MNT

## Format attendu

OpenVigie lit une grille NumPy plus un JSON de géoréférencement. Le cœur embarqué
n'emporte **aucune bibliothèque de projection** : la conversion se fait une fois,
sur un poste outillé, pas sur la passerelle du site.

```json
{
  "origin_x": 2.9955, "origin_y": 44.0045,
  "pixel_x": 0.0000125, "pixel_y": -0.0000090,
  "crs": "geographic", "nodata": -99999.0
}
```

## Coordonnées projetées : refusées explicitement

`crs: "projected"` est **rejeté** par le ray-casting depuis la 0.4.0
(AUDIT P0-09). Une dalle Lambert-93, dont l'origine est de l'ordre de
700 000 / 6 600 000, interrogée avec des coordonnées 3 / 44 ne provoquait aucune
erreur : elle renvoyait un terrain arbitraire mais parfaitement plausible, donc
une localisation convaincante et fausse.

Convertir une dalle IGN en WGS84 à la préparation :

```python
# poste de préparation uniquement — rasterio et pyproj ne sont pas des
# dépendances d'OpenVigie
import numpy as np, rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
from openvigie.dem import DEM, GeoTransform

with rasterio.open("RGEALTI_lambert93.tif") as src:
    transform, w, h = calculate_default_transform(
        src.crs, "EPSG:4326", src.width, src.height, *src.bounds
    )
    dst = np.empty((h, w), dtype=np.float32)
    reproject(rasterio.band(src, 1), dst,
              src_transform=src.transform, src_crs=src.crs,
              dst_transform=transform, dst_crs="EPSG:4326",
              resampling=Resampling.bilinear)

DEM(
    elevation=dst.astype(np.float64),
    transform=GeoTransform(
        origin_x=transform.c + transform.a / 2,   # centre du premier pixel
        origin_y=transform.f + transform.e / 2,
        pixel_x=transform.a, pixel_y=transform.e,
        crs="geographic",
    ),
    nodata=src.nodata,
).save("data/mnt/site-01.npy")
```

## Métadonnées à conserver

Le format minimal ne les porte pas encore ; les consigner à côté du MNT :

- EPSG source et EPSG cible ;
- convention d'origine (centre ou coin de pixel) — une erreur d'un demi-pixel à
  1 m de résolution est négligeable, à 25 m elle ne l'est plus ;
- résolution native et méthode de rééchantillonnage ;
- référentiel vertical (RGF93/IGN69 en France ; l'écart au géoïde atteint
  plusieurs dizaines de mètres) ;
- date et source de la dalle ;
- altitude de la caméra, mesurée et non déduite du MNT si le mât est sur un
  ouvrage.

## Vérification

Avant d'exploiter une localisation, contrôler le MNT sur des amers relevés —
sommets, pylônes, clochers — dont les coordonnées sont connues :

```bash
openvigie viewshed -c site.yaml --dem data/mnt/site-01.npy
```

Une crête bien placée en azimut mais à 15 % de la bonne distance signale
généralement une erreur d'altitude de caméra ou de référentiel vertical, pas une
erreur de projection.
