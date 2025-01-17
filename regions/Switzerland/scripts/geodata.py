import numpy as np
import os
from os import listdir
from os.path import isfile, join
import xarray as xr
import geopandas as gpd
from shapely.geometry import Point, box
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import calculate_default_transform, reproject, Resampling
from rasterio.merge import merge
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree
from datetime import datetime
from collections import defaultdict
from dateutil.relativedelta import relativedelta
from sklearn.neighbors import NearestNeighbors


def toRaster(gdf, lon, lat, file_name, source_crs='EPSG:4326'):
    # Assuming your GeoDataFrame is named gdf
    # Define the grid dimensions and resolution based on your data
    nrows, ncols = lat.shape[0], lon.shape[
        0]  # Adjust based on your latitude and longitude resolution

    data_array = np.full((nrows, ncols), np.nan)  # Initialize with NaNs

    # Create a raster transformation for the grid (assuming lat/lon range)
    transform = from_origin(min(lon), max(lat), abs(lon[1] - lon[0]),
                            abs(lat[1] - lat[0]))

    # Populate the raster data
    for index, row in gdf.iterrows():
        # Assuming row['geometry'].x and row['geometry'].y give you lon and lat
        lon_idx = np.argmin(np.abs(lon - row.geometry.x))
        lat_idx = np.argmin(np.abs(lat - row.geometry.y))
        data_array[lat_idx, lon_idx] = row['pred_masked']

    # Save the raster
    with rasterio.open(
            file_name,
            'w',
            driver='GTiff',
            height=data_array.shape[0],
            width=data_array.shape[1],
            count=1,
            dtype=data_array.dtype,
            crs=source_crs,
            transform=transform,
    ) as dst:
        dst.write(data_array, 1)

    # Read the raster data
    with rasterio.open(file_name) as src:
        raster_data = src.read(1)  # Read the first band
        extent = [
            src.bounds.left, src.bounds.right, src.bounds.bottom,
            src.bounds.top
        ]
    return raster_data, extent


def reproject_raster_to_lv95(
        input_raster,
        output_raster,
        dst_crs='EPSG:2056'  # Destination CRS (Swiss LV95) or EPSG:21781
):
    # Define the source and destination CRS
    src_crs = 'EPSG:4326'  # Original CRS (lat/lon)

    # Open the source raster
    with rasterio.open(input_raster) as src:
        # Calculate the transform and dimensions for the destination CRS
        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds)

        # Set up the destination raster metadata
        dst_meta = src.meta.copy()
        dst_meta.update({
            'crs': dst_crs,
            'transform': transform,
            'width': width,
            'height': height
        })

        # Perform the reprojection
        with rasterio.open(output_raster, 'w', **dst_meta) as dst:
            for i in range(1, src.count + 1):  # Iterate over each band
                # reproject(
                #     source=rasterio.band(src, i),
                #     destination=rasterio.band(dst, i),
                #     src_transform=src.transform,
                #     src_crs=src.crs,
                #     dst_transform=transform,
                #     dst_crs=dst_crs,
                #     resampling=Resampling.nearest  # You can also use other methods, like bilinear
                # )
                # Create an array to hold the reprojected data
                data = np.empty((height, width), dtype=src.meta['dtype'])

                reproject(
                    source=rasterio.band(src, i),
                    destination=data,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=dst_crs,
                    resampling=Resampling.
                    nearest  # You can also use other methods, like bilinear
                )

                # Replace any 0 values in `data` with NaN
                data[data == 0] = np.nan

                # Write the modified data to the output raster band
                dst.write(data, i)


def merge_rasters(raster1_path, raster2_path, output_path):
    # Open the rasters
    raster_files = [raster1_path, raster2_path]
    src_files_to_mosaic = [rasterio.open(fp) for fp in raster_files]

    # Merge the rasters
    mosaic, out_transform = merge(src_files_to_mosaic)

    # Update the metadata to match the mosaic output
    out_meta = src_files_to_mosaic[0].meta.copy()
    out_meta.update({
        "driver": "GTiff",
        "height": mosaic.shape[1],
        "width": mosaic.shape[2],
        "transform": out_transform,
        "crs": src_files_to_mosaic[0].crs
    })
    # replace 0 values with NaN
    mosaic[mosaic == 0] = np.nan

    # Write the mosaic raster to disk
    with rasterio.open(output_path, "w", **out_meta) as dest:
        dest.write(mosaic)

    # Close all source files
    for src in src_files_to_mosaic:
        src.close()


def GaussianFilter(ds, variable_name='pred_masked', sigma=1):
    """
    Apply Gaussian filter only to the specified variable in the xarray.Dataset.
    
    Parameters:
    - ds (xarray.Dataset): Input dataset
    - variable_name (str): The name of the variable to apply the filter to (default 'pred_masked')
    - sigma (float): The standard deviation for the Gaussian filter
    
    Returns:
    - xarray.Dataset: New dataset with smoothed variable
    """
    # Check if the variable exists in the dataset
    if variable_name not in ds:
        raise ValueError(
            f"Variable '{variable_name}' not found in the dataset.")

    # Get the DataArray for the specified variable
    data_array = ds[variable_name]

    # Step 1: Create a mask of valid data (non-NaN values)
    mask = ~np.isnan(data_array)

    # Step 2: Replace NaNs with zero (or a suitable neutral value)
    filled_data = data_array.fillna(0)

    # Step 3: Apply Gaussian filter to the filled data
    smoothed_data = gaussian_filter(filled_data, sigma=sigma)

    # Step 4: Restore NaNs to their original locations
    smoothed_data = xr.DataArray(smoothed_data,
                                 dims=data_array.dims,
                                 coords=data_array.coords,
                                 attrs=data_array.attrs).where(
                                     mask)  # Apply the mask to restore NaNs

    # Create a new dataset with the smoothed data
    smoothed_dataset = ds.copy()  # Make a copy of the original dataset
    smoothed_dataset[
        variable_name] = smoothed_data  # Replace the original variable with the smoothed one

    return smoothed_dataset


def TransformToRaster(filename_nc,
                      filename_tif,
                      path_nc_wgs84,
                      path_tif_wgs84,
                      path_tif_lv95,
                      dst_crs='EPSG:2056'):
    ds_latlon = xr.open_dataset(path_nc_wgs84 + filename_nc)

    # Smoothing
    ds_latlon_g = GaussianFilter(ds_latlon)

    # Convert to GeoPandas
    gdf, lon, lat = toGeoPandas(ds_latlon_g)

    # Reproject to LV95 (EPSG:2056) swiss coordinates
    # gdf_lv95 = gdf.to_crs("EPSG:2056")

    createPath(path_tif_wgs84)
    createPath(path_tif_lv95)

    # Convert to raster and save
    raster_data, extent = toRaster(gdf,
                                   lon,
                                   lat,
                                   file_name=path_tif_wgs84 + filename_tif)

    # Reproject raster to Swiss coordinates (LV95)
    reproject_raster_to_lv95(path_tif_wgs84 + filename_tif,
                             path_tif_lv95 + filename_tif,
                             dst_crs=dst_crs)

    # Make classes map of snow/ice:
    # Replace values: below 0 with 3, above 0 with 1
    gdf_class = gdf.copy()
    tol = 0
    gdf_class.loc[gdf['pred_masked'] <= 0 + tol, 'pred_masked'] = 3
    gdf_class.loc[gdf['pred_masked'] > 0 + tol, 'pred_masked'] = 1

    path_class_tif_lv95 = path_tif_lv95 + 'classes/'
    path_class_tif_wgs84 = path_tif_wgs84 + 'classes/'

    createPath(path_class_tif_lv95)
    createPath(path_class_tif_wgs84)

    # Convert to raster and save
    raster_data, extent = toRaster(gdf_class,
                                   lon,
                                   lat,
                                   file_name=path_class_tif_wgs84 +
                                   filename_tif)

    # Reproject raster to Swiss coordinates (LV95)
    reproject_raster_to_lv95(path_class_tif_wgs84 + filename_tif,
                             path_class_tif_lv95 + filename_tif,
                             dst_crs=dst_crs)

    return gdf, gdf_class, raster_data, extent


def createPath(path):
    if not os.path.exists(path):
        os.makedirs(path)


# empties a folder
def emptyfolder(path):
    if os.path.exists(path):
        onlyfiles = [f for f in os.listdir(path) if isfile(join(path, f))]
        for f in onlyfiles:
            os.remove(path + f)
    else:
        createPath(path)


def get_hydro_year_and_month(file_date):
    if file_date.day < 15:
        # Move to the first day of the previous month
        file_date -= relativedelta(months=1)  # Move to the previous month
        file_date = file_date.replace(
            day=1)  # Set the day to the 1st of the previous month
    else:
        # Move to the first day of the current month
        file_date = file_date.replace(
            day=1)  # Set the day to the 1st of the current month

    # Step 2: Determine the closest month
    closest_month = file_date.strftime(
        '%b').lower()  # Get the full name of the month

    # Step 3: Determine the hydrological year
    # Hydrological year runs from September to August
    if file_date.month >= 9:  # September, October, November, December
        hydro_year = file_date.year + 1  # Assign to the next year
    else:  # January to August
        hydro_year = file_date.year  # Assign to the current year

    return closest_month, hydro_year


def organize_rasters_by_hydro_year(path_S2, satellite_years):
    rasters = defaultdict(
        lambda: defaultdict(list))  # Nested dictionary for years and months

    for year in satellite_years:
        folder_path = os.path.join(path_S2, str(year))
        for f in os.listdir(folder_path):
            if f.endswith(".tif"):  # Only process raster files
                # Step 1: Extract the date from the string
                date_str = f.split(
                    '_')[3][:8]  # Extract the 8-digit date (YYYYMMDD)
                file_date = datetime.strptime(
                    date_str, "%Y%m%d")  # Convert to datetime object

                closest_month, hydro_year = get_hydro_year_and_month(file_date)
                if hydro_year < 2022:
                    rasters[hydro_year][closest_month].append(f)

    return rasters


def IceSnowCover(gdf_class, gdf_class_raster):
    # Exclude pixels with "classes" 5 (cloud) in gdf_class_raster
    valid_classes = gdf_class[gdf_class_raster.classes != 5]

    # Calculate percentage of snow cover (class 1) in valid classes
    snow_cover_glacier = valid_classes.classes[
        valid_classes.classes == 1].count() / valid_classes.classes.count()

    return snow_cover_glacier


def replace_clouds_with_nearest_neighbor(gdf,
                                         class_column='classes',
                                         cloud_class=5):
    """
    Replace cloud pixels in a GeoDataFrame with the most common class among their 
    nearest neighbors, excluding NaN values.

    Parameters:
    - gdf (GeoDataFrame): GeoPandas DataFrame containing pixel data with a geometry column.
    - class_column (str): The column name representing the class of each pixel (integer classes).
    - cloud_class (int): The class to be replaced (e.g., 1 for cloud).
    - n_neighbors (int): The number of nearest neighbors to consider for majority voting.

    Returns:
    - GeoDataFrame: Updated GeoDataFrame with cloud classes replaced.
    """
    # Separate cloud pixels and non-cloud pixels
    cloud_pixels = gdf[gdf[class_column] == cloud_class]
    non_cloud_pixels = gdf[gdf[class_column] != cloud_class]

    # Remove NaN values from non-cloud pixels
    non_cloud_pixels = non_cloud_pixels[non_cloud_pixels[class_column].notna()]

    # If no clouds or no non-NaN non-cloud pixels, return the original GeoDataFrame
    if cloud_pixels.empty or non_cloud_pixels.empty:
        return gdf

    # Extract coordinates for nearest-neighbor search
    cloud_coords = np.array(
        list(cloud_pixels.geometry.apply(lambda geom: (geom.x, geom.y))))
    non_cloud_coords = np.array(
        list(non_cloud_pixels.geometry.apply(lambda geom: (geom.x, geom.y))))

    # Perform nearest-neighbor search
    nbrs = NearestNeighbors(n_neighbors=1,
                            algorithm='auto').fit(non_cloud_coords)
    distances, indices = nbrs.kneighbors(cloud_coords)

    # Map nearest neighbor's class to cloud pixels
    nearest_classes = non_cloud_pixels.iloc[
        indices.flatten()][class_column].values
    gdf.loc[cloud_pixels.index, class_column] = nearest_classes

    return gdf


def snowline(gdf, class_value=1, percentage_threshold=20):
    """
    Find the first elevation band where the percentage of the given class exceeds the specified threshold
    and add a boolean column to gdf indicating the selected band.
    
    Parameters:
    - gdf (GeoDataFrame): Input GeoDataFrame with 'elev_band' and 'classes' columns
    - class_value (int): The class value to check for (default is 1 for snow)
    - percentage_threshold (float): The percentage threshold to exceed (default is 20%)
    
    Returns:
    - gdf (GeoDataFrame): GeoDataFrame with an additional boolean column indicating the selected elevation band
    - first_band (int): The first elevation band that meets the condition
    """
    # Step 1: Group by elevation band and calculate the percentage of 'class_value' in each band
    band_class_counts = gdf.groupby('elev_band')['classes'].value_counts(
        normalize=True)

    # Step 2: Calculate the percentage of the specified class in each band
    class_percentage = band_class_counts.xs(
        class_value, level=1) * 100  # Multiply by 100 to convert to percentage

    # Step 3: Find the first band where the class percentage exceeds the threshold
    first_band = None
    for elev_band, percentage in class_percentage.items():
        if percentage >= percentage_threshold:
            first_band = elev_band
            break

    if first_band is not None:
        # Step 4: Add a new column to the GeoDataFrame to indicate the first elevation band
        gdf['selected_band'] = gdf['elev_band'] == first_band
    else:
        # If no band meets the threshold, the new column will be False for all rows
        gdf['selected_band'] = False

    return gdf, first_band


def classify_elevation_bands(gdf_glacier, band_size=50):
    """
    Classify elevation into bands based on the 'elev_masked' column in the GeoDataFrame.

    Parameters:
        gdf_glacier (GeoDataFrame): A GeoDataFrame containing an 'elev_masked' column.
        band_size (int): The size of each elevation band.

    Returns:
        GeoDataFrame: The input GeoDataFrame with an additional 'elev_band' column.
    """
    # Ensure the 'elev_masked' column exists and contains valid data
    if 'elev_masked' not in gdf_glacier.columns:
        raise ValueError("GeoDataFrame does not contain 'elev_masked' column")

    # Handle NaN values in 'elev_masked' and classify into elevation bands
    gdf_glacier['elev_band'] = (
        gdf_glacier['elev_masked']
        .fillna(-1)  # Replace NaN with a placeholder (e.g., -1 or another value)
        .floordiv(band_size) * band_size  # Calculate the elevation band
    )

    # Optionally set the 'elev_band' of NaN entries back to NaN
    gdf_glacier.loc[gdf_glacier['elev_masked'].isna(), 'elev_band'] = None

    return gdf_glacier


def AddSnowline(gdf_glacier_corr, band_size=100, percentage_threshold=50):
    # Add snowline
    # Remove weird border effect
    #gdf_glacier_corr = gdf_glacier_corr[gdf_glacier_corr.dis_masked > 10]
    
    gdf_glacier_corr = classify_elevation_bands(gdf_glacier_corr, band_size)

    snowline(gdf_glacier_corr,
             class_value=1,
             percentage_threshold=percentage_threshold)

    return gdf_glacier_corr
