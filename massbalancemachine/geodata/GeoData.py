import pandas as pd
import config
import xarray as xr
import numpy as np
import salem
import pyproj
from scipy.ndimage import gaussian_filter
import os
import rasterio
from shapely.geometry import Point, box
import geopandas as gpd
from scipy.spatial import cKDTree


class GeoData:
    """Class for handling geodata objects such as raster files (nc, tif),
       xarray datasets, shapefiles and geopandas dataframes.
       Attributes:
       - data (pd.DataFrame): Dataframe with the monthly point MB predictions.
       - ds_latlon (xr.Dataset): Dataset with the predictions in WGS84 coordinates.
       - ds_xy (xr.Dataset): Dataset with the predictions in the projection of OGGM. For CH, this is LV95.
       - gdf (gpd.GeoDataFrame): Geopandas dataframe with the predictions in WGS84 coordinates.
    """

    def __init__(
        self,
        data: pd.DataFrame,
    ):
        self.data = data
        self.ds_latlon = None
        self.ds_xy = None
        self.gdf = None

    def set_gdf(self, gdf):
        """Set the gdf attribute to a geopandas dataframe."""
        self.gdf = gdf

    def set_ds_latlon(self, ds_or_path, path_nc_wgs84=""):
        """Set the ds_latlon attribute to an xarray Dataset or load it from a NetCDF file."""
        if isinstance(ds_or_path, xr.Dataset):
            # If the input is an xarray.Dataset, use it directly
            self.ds_latlon = ds_or_path
        elif isinstance(ds_or_path, str):
            # If the input is a string and corresponds to a valid file, open the xarray from the file
            full_path = os.path.join(path_nc_wgs84, ds_or_path)
            self.ds_latlon = xr.open_dataset(full_path)
        else:
            raise TypeError(
                "ds_latlon must be either an xarray.Dataset or a valid file path to a NetCDF file"
            )
        # Apply Gaussian filter
        self.apply_gaussian_filter()
        
        # Convert to geopandas
        self.xr_to_gpd()

    def set_ds_xy(self, ds_or_path, path_nc_xy=""):
        """Set the ds_xy attribute to an xarray Dataset or load it from a NetCDF file."""
        if isinstance(ds_or_path, xr.Dataset):
            # If the input is an xarray.Dataset, use it directly
            self.ds_xy = ds_or_path
        elif isinstance(ds_or_path, str) and os.path.isfile(
                os.path.join(path_nc_xy, ds_or_path)):
            # If the input is a string and corresponds to a valid file, open the xarray from the file
            full_path = os.path.join(path_nc_xy, ds_or_path)
            self.ds_xy = xr.open_dataset(full_path)
        else:
            raise TypeError(
                "ds_xy must be either an xarray.Dataset or a valid file path to a NetCDF file"
            )

    def pred_to_xr(self, ds, gdir, pred_var='pred'):
        """Transforms MB predictions to xarray dataset. 
           Makes it easier for plotting and saving to netcdf.
           Keeps on netcdf in OGGM projection and transforms one to WGS84.

        Args:
            ds (xarray.Dataset): OGGM glacier grid with attributes.
            gdir gdir (oggm.GlacierDirectory): the OGGM glacier directory
            pred_var (str, optional): Name of prediction column in self.data. Defaults to 'pred'.
        """
        glacier_indices = np.where(ds['glacier_mask'].values == 1)
        pred_masked = ds.glacier_mask.values

        # Set pred_masked to nan where 0
        pred_masked = np.where(pred_masked == 0, np.nan, pred_masked)
        for i, (x_index, y_index) in enumerate(
                zip(glacier_indices[0], glacier_indices[1])):
            pred_masked[x_index, y_index] = self.data.iloc[i][pred_var]

        pred_masked = np.where(pred_masked == 1, np.nan, pred_masked)
        self.ds_xy = ds.assign(pred_masked=(('y', 'x'), pred_masked))

        # Change from OGGM proj. to wgs84
        self.ds_latlon = self.oggmToWgs84(self.ds_xy, gdir)

    def save_arrays(self, path_wgs84: str, path_lv95: str, filename: str):
        """Saves the xarray datasets in OGGM projection and WGMS84 to netcdf files.

        Args:
            path_wgs84 (str): path to save the dataset in WGS84 projection.
            path_lv95 (str): path to save the dataset in LV95 projection.
            filename (str): filename for the netcdf file.
        """
        self.__class__.save_to_netcdf(self.ds_latlon, path_wgs84, filename)
        self.__class__.save_to_netcdf(self.ds_xy, path_lv95, filename)

    def xr_to_gpd(self):
        # Get lat and lon, and variables data
        lat = self.ds_latlon['latitude'].values
        lon = self.ds_latlon['longitude'].values
        pred_masked_data = self.ds_latlon['pred_masked'].values
        masked_elev_data = self.ds_latlon['masked_elev'].values
        masked_dis_data = self.ds_latlon['masked_dis'].values

        # Create meshgrid of coordinates
        lon_grid, lat_grid = np.meshgrid(lon, lat)

        # Flatten all arrays to match shapes
        lon_flat = lon_grid.flatten()
        lat_flat = lat_grid.flatten()
        pred_masked_data_flat = pred_masked_data.flatten()
        masked_elev_data_flat = masked_elev_data.flatten()
        masked_dis_data_flat = masked_dis_data.flatten()

        # Verify shapes
        assert len(lon_flat) == len(lat_flat) == len(
            pred_masked_data_flat) == len(
                masked_elev_data_flat), "Shapes don't match!"

        # Create GeoDataFrame
        points = [Point(xy) for xy in zip(lon_flat, lat_flat)]
        gdf = gpd.GeoDataFrame(
            {
                "pred_masked": pred_masked_data_flat,
                "elev_masked": masked_elev_data_flat,
                "dis_masked": masked_dis_data_flat
            },
            geometry=points,
            crs="EPSG:4326")

        #return gdf, lon, lat
        self.gdf = gdf
        
    def apply_gaussian_filter(self, variable_name: str = 'pred_masked', sigma: float = 1):
        """
        Apply Gaussian filter only to the specified variable in the xarray.Dataset.

        Parameters:
        - variable_name (str): The name of the variable to apply the filter to (default 'pred_masked')
        - sigma (float): The standard deviation for the Gaussian filter. Default is 1.
        
        Returns:
        - self: Returns the instance for method chaining.
        """
        if self.ds_latlon is None:
            raise ValueError("ds_latlon attribute is not set. Please set it first.")
        
        # Check if the variable exists in the dataset
        if variable_name not in self.ds_latlon:
            raise ValueError(f"Variable '{variable_name}' not found in the dataset.")

        # Get the DataArray for the specified variable
        data_array = self.ds_latlon[variable_name]

        # Step 1: Create a mask of valid data (non-NaN values)
        mask = ~np.isnan(data_array)

        # Step 2: Replace NaNs with zero (or a suitable neutral value)
        filled_data = data_array.fillna(0)

        # Step 3: Apply Gaussian filter to the filled data
        smoothed_data = gaussian_filter(filled_data, sigma=sigma)

        # Step 4: Restore NaNs to their original locations
        smoothed_data = xr.DataArray(
            smoothed_data,
            dims=data_array.dims,
            coords=data_array.coords,
            attrs=data_array.attrs).where(mask)  # Apply the mask to restore NaNs

        # Replace the original variable with the smoothed one in the dataset
        self.ds_latlon[variable_name] = smoothed_data
        
        # Return self to allow method chaining
        return self
    
    def classify_snow_cover(self, tol = 0.1):
        # Apply classification logic:
        # - If pred_masked > -tol, assign 1 (snow)
        # - If pred_masked <= -tol, assign 3 (ice)
        # - If pred_masked is NaN, assign NaN
        self.gdf['classes'] = np.where(
            self.gdf['pred_masked'] > -tol, 1,
            np.where(self.gdf['pred_masked'] <= -tol, 3, np.nan))

    @staticmethod
    def save_to_netcdf(ds: xr.Dataset, path: str, filename: str):
        """Saves the xarray dataset to a netcdf file.
        """
        # Create path if not exists
        if not os.path.exists(path):
            os.makedirs(path)

        # delete file if already exists
        if os.path.exists(path + filename):
            os.remove(path + filename)

        # save prediction to netcdf
        ds.to_netcdf(path + filename)
        
    @staticmethod
    def oggmToWgs84(ds, gdir):
        """Transforms a xarray dataset from OGGM projection to WGS84.

        Args:
            ds (xr.Dataset): xr.Dataset with the predictions in OGGM projection.
            gdir (oggm.GlacierDirectory): oggm glacier directory

        Returns:
            xr.Dataset: xr.Dataset with the predictions in WGS84 projection.
        """
        # Define the Swiss coordinate system (EPSG:2056) and WGS84 (EPSG:4326)
        transformer = pyproj.Transformer.from_proj(gdir.grid.proj,
                                                   salem.wgs84,
                                                   always_xy=True)

        # Get the Swiss x and y coordinates from the dataset
        x_coords = ds['x'].values
        y_coords = ds['y'].values

        # Create a meshgrid for all x, y coordinate pairs
        x_mesh, y_mesh = np.meshgrid(x_coords, y_coords)

        # Flatten the meshgrid arrays for transformation
        x_flat = x_mesh.ravel()
        y_flat = y_mesh.ravel()

        # Transform the flattened coordinates
        lon_flat, lat_flat = transformer.transform(x_flat, y_flat)

        # Reshape transformed coordinates back to the original grid shape
        lon = lon_flat.reshape(x_mesh.shape)
        lat = lat_flat.reshape(y_mesh.shape)

        # Extract unique 1D coordinates for lat and lon
        lon_1d = lon[
            0, :]  # Take the first row for unique longitudes along x-axis
        lat_1d = lat[:,
                     0]  # Take the first column for unique latitudes along y-axis

        # Assign the 1D coordinates to x and y dimensions
        ds = ds.assign_coords(longitude=("x", lon_1d), latitude=("y", lat_1d))

        # Swap x and y dimensions with lon and lat
        ds = ds.swap_dims({"x": "longitude", "y": "latitude"})

        # Optionally, drop the old x and y coordinates if no longer needed
        ds = ds.drop_vars(["x", "y"])

        return ds

    @staticmethod
    def raster_to_gpd(input_raster):
        # Open the raster
        with rasterio.open(input_raster) as src:
            data = src.read(1)  # Read first band
            transform = src.transform
            crs = src.crs

        # Get indices of non-NaN values
        rows, cols = np.where(data != src.nodata)
        values = data[rows, cols]

        # Convert raster cells to points
        points = [
            Point(transform * (col + 0.5, row + 0.5))
            for row, col in zip(rows, cols)
        ]

        # Create GeoDataFrame
        gdf_raster = gpd.GeoDataFrame({"classes": values},
                                      geometry=points,
                                      crs=crs)
        return gdf_raster


    @staticmethod
    def resample_satellite_to_glacier(gdf_glacier, gdf_raster):
        # Clip raster to glacier extent
        # Step 1: Get the bounding box of the points GeoDataFrame
        bounding_box = gdf_glacier.total_bounds  # [minx, miny, maxx, maxy]
        raster_bounds = gdf_raster.total_bounds  # [minx, miny, maxx, maxy]

        # Problem 1: check if glacier bounds are within raster bounds
        if not (bounding_box[0] >= raster_bounds[0]
                and  # minx of glacier >= minx of raster
                bounding_box[1] >= raster_bounds[1]
                and  # miny of glacier >= miny of raster
                bounding_box[2] <= raster_bounds[2]
                and  # maxx of glacier <= maxx of raster
                bounding_box[3]
                <= raster_bounds[3]  # maxy of glacier <= maxy of raster
                ):
            return 0

        # Step 2: Create a rectangular geometry from the bounding box
        bbox_polygon = box(*bounding_box)

        # Problem 2: Glacier is in regions where raster is NaN
        gdf_clipped = gpd.clip(gdf_raster, bbox_polygon)
        if gdf_clipped.empty:
            return 1

        # Step 3: Clip the raster-based GeoDataFrame to this bounding box
        gdf_clipped = gdf_raster[gdf_raster.intersects(bbox_polygon)]

        # Optionally, further refine the clipping if exact match is needed
        gdf_clipped = gpd.clip(gdf_raster, bbox_polygon)

        # Resample clipped raster to glacier points
        # Extract coordinates and values from gdf_clipped
        clipped_coords = np.array([(geom.x, geom.y)
                                   for geom in gdf_clipped.geometry])
        clipped_values = gdf_clipped['classes'].values

        # Extract coordinates from gdf_glacier
        points_coords = np.array([(geom.x, geom.y)
                                  for geom in gdf_glacier.geometry])

        # Build a KDTree for efficient nearest-neighbor search
        tree = cKDTree(clipped_coords)

        # Query the tree for the nearest neighbor to each point in gdf_glacier
        distances, indices = tree.query(points_coords)

        # Assign the values from the nearest neighbors
        gdf_clipped_res = gdf_glacier.copy()
        gdf_clipped_res = gdf_clipped_res[['geometry']]
        gdf_clipped_res['classes'] = clipped_values[indices]

        # Assuming 'value' is the column storing the resampled values
        gdf_clipped_res['classes'] = np.where(
            gdf_glacier['pred_masked'].isna(
            ),  # Check where original values are NaN
            np.nan,  # Assign NaN to those locations
            gdf_clipped_res['classes'],  # Keep the resampled values elsewhere
        )

        return gdf_clipped_res