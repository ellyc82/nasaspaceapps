# NASA-Powered Smart Crop Rotation Planner

The current increment connects the existing crop planner to NASA POWER's daily
point API. It retrieves mean, maximum, and minimum temperature, corrected
precipitation, solar radiation, and relative humidity for a coordinate and
date range entered by the user. NASA POWER is called by the Flask backend; no
NASA or Earthdata credential is placed in browser code.

## Run locally

1. Use Python 3.10 or later.
2. Install the backend dependency: `py -m pip install -r requirements.txt`
3. Start the server: `py app.py`
4. Open `http://127.0.0.1:5000`.

Enter latitude/longitude and dates (1981-01-01 through today, up to 366 days).
NASA POWER's daily service can be near-real-time, so very recent dates may not
yet have values. Repeated identical queries are cached in memory for one hour.

Run the backend tests with `py -m unittest`.

## Data and current limitations

- NASA POWER values are live API results. The interface displays missing NASA
  `-999` values as unavailable and reports the count of valid days.
- Temperature comparisons use the crop table's draft optimum ranges. They are
  climate screening only, not a planting-date or yield guarantee.
- Rainfall is shown separately from crop water demand. It is not treated as
  irrigation or as total water available to a crop.
- The spreadsheet says its crop values are unverified local estimates; verify
  them with UC ANR / UC Cooperative Extension.
- Soil moisture (SMAP), USDA soil data, mapping/geocoding, nutrient/rotation
  logic, and optional ECOSTRESS/GPM integrations are not connected yet. Their
  UI areas are identified as pending rather than filled with simulated data.
# nasaspaceapps
