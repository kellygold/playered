"""PyInstaller entry; dispatch spawned workers before importing the application."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    import sys

    if sys.argv[1:] == ["--self-test"]:
        import json
        import numpy
        import triangle
        from PIL import Image
        from image23mf.parallel import process_map
        from image23mf.profiles import ProfileCatalogService

        geometry = triangle.triangulate({"vertices": numpy.array([[0, 0], [1, 0], [0, 1]])})
        assert len(geometry["triangles"]) == 1
        assert process_map(abs, [-1, -2, -3], max_workers=2) == [1, 2, 3]
        assert Image.new("RGB", (4, 4)).size == (4, 4)
        assert ProfileCatalogService.bundled().catalog
        print(json.dumps({"frozen": bool(getattr(sys, "frozen", False)),
                          "numpy": numpy.__version__, "triangle": "passed",
                          "multiprocessing": "passed", "profiles": "passed"}))
    else:
        from image23mf.macos.desktop import main

        raise SystemExit(main())
