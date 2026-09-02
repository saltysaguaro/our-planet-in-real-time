# Northern Algeria focus — NASA SVS 5666

This branch contains an automated, provenance-preserving reframe of NASA Scientific Visualization Studio item 5666, **Smoke from Record-Breaking Wildfires in Spain and France**.

The GitHub Actions workflow downloads NASA's official 1920×1080 movie, crops the view to the western Mediterranean and northern Algeria, restores the original legend, UTC timestamp, and NASA/GMAO credit panels from the same source frames, and exports an H.264 MP4. It does not recalculate, recolor, interpolate, or otherwise alter the model-derived smoke values.

The complete internal NASA SVS production project is not publicly distributed. Consequently, this is intentionally a spatial reframe of the authors' released scientific render—not an independently reconstructed rendering pipeline.
