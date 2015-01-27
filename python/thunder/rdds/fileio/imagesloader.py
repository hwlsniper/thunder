"""Provides ImagesLoader object and helpers, used to read Images data from disk or other filesystems.
"""
from matplotlib.pyplot import imread
from io import BytesIO
from numpy import array, dstack, frombuffer, ndarray, prod
from thunder.rdds.fileio.readers import getParallelReaderForPath
from thunder.rdds.images import Images


class ImagesLoader(object):
    """Loader object used to instantiate Images data stored in a variety of formats.
    """
    def __init__(self, sparkContext):
        """Initialize a new ImagesLoader object.

        Parameters
        ----------
        sparkcontext: SparkContext
            The pyspark SparkContext object used by the current Thunder environment.
        """
        self.sc = sparkContext

    def fromArrays(self, arrays):
        """Load Images data from passed sequence of numpy arrays.

        Expected usage is mainly in testing - having a full dataset loaded in memory
        on the driver is likely prohibitive in the use cases for which Thunder is intended.
        """
        # if passed a single array, cast it to a sequence of length 1
        if isinstance(arrays, ndarray):
            arrays = [arrays]

        shape = None
        dtype = None
        for ary in arrays:
            if shape is None:
                shape = ary.shape
                dtype = ary.dtype
            if not ary.shape == shape:
                raise ValueError("Arrays must all be of same shape; got both %s and %s" %
                                 (str(shape), str(ary.shape)))
            if not ary.dtype == dtype:
                raise ValueError("Arrays must all be of same data type; got both %s and %s" %
                                 (str(dtype), str(ary.dtype)))
        return Images(self.sc.parallelize(enumerate(arrays), len(arrays)),
                      dims=shape, dtype=str(dtype), nimages=len(arrays))

    def fromStack(self, dataPath, dims, dtype='int16', ext='stack', startIdx=None, stopIdx=None, recursive=False,
                  nplanes=None):
        """Load an Images object stored in a directory of flat binary files

        The RDD wrapped by the returned Images object will have a number of partitions equal to the number of image data
        files read in by this method.

        Currently all binary data read by this method is assumed to be formatted as signed 16 bit integers in native
        byte order.

        Parameters
        ----------

        dataPath: string
            Path to data files or directory, specified as either a local filesystem path or in a URI-like format,
            including scheme. A datapath argument may include a single '*' wildcard character in the filename.

        dims: tuple of positive int
            Dimensions of input image data, ordered with fastest-changing dimension first

        ext: string, optional, default "stack"
            Extension required on data files to be loaded.

        startIdx, stopIdx: nonnegative int. optional.
            Indices of the first and last-plus-one data file to load, relative to the sorted filenames matching
            `datapath` and `ext`. Interpreted according to python slice indexing conventions.

        recursive: boolean, default False
            If true, will recursively descend directories rooted at datapath, loading all files in the tree that
            have an extension matching 'ext'. Recursive loading is currently only implemented for local filesystems
            (not s3).

        nplanes: positive integer, default None
            If passed, will cause a single binary stack file to be subdivided into multiple time points. Every
            `nplanes` image planes in the file (after reshaping to dims) will be considered as a new time point. With
            nplanes=None (the default), a single file will be considered to represent a single time point.
        """
        if not dims:
            raise ValueError("Image dimensions must be specified if loading from binary stack data")

        if nplanes is not None and nplanes <= 0:
            raise ValueError("nplanes must be positive if passed, got %d" % nplanes)

        def toArray(idxAndBuf):
            idx, buf = idxAndBuf
            ary = frombuffer(buf, dtype=dtype, count=int(prod(dims))).reshape(dims, order='F')
            if nplanes is None:
                yield idx, ary
            else:
                # divide array into chunks of nplanes
                npoints = dims[-1] / nplanes  # integer division
                if dims[-1] % nplanes:
                    npoints += 1
                timepoint = 0
                lastPlane = 0
                curPlane = 1
                while curPlane < ary.shape[-1]:
                    if curPlane % nplanes == 0:
                        slices = [slice(None)] * (ary.ndim - 1) + [slice(lastPlane, curPlane)]
                        yield idx*npoints + timepoint, ary[slices]
                        timepoint += 1
                        lastPlane = curPlane
                    curPlane += 1
                # yield remaining planes
                slices = [slice(None)] * (ary.ndim - 1) + [slice(lastPlane, ary.shape[-1])]
                yield idx*npoints + timepoint, ary[slices]

        reader = getParallelReaderForPath(dataPath)(self.sc)
        readerRdd = reader.read(dataPath, ext=ext, startIdx=startIdx, stopIdx=stopIdx, recursive=recursive)
        nimages = reader.lastNRecs if nplanes is None else None
        newDims = tuple(list(dims[:-1]) + [nplanes]) if nplanes else dims
        return Images(readerRdd.flatMap(toArray), nimages=nimages, dims=newDims, dtype=dtype)

    def fromTif(self, dataPath, ext='tif', startIdx=None, stopIdx=None, recursive=False, nplanes=None):
        """Sets up a new Images object with data to be read from one or more tif files.

        This method attempts to explicitly import PIL. ImportError may be thrown if 'from PIL import Image' is
        unsuccessful. (PIL/pillow is not an explicit requirement for thunder.)

        The RDD wrapped by the returned Images object will have a number of partitions equal to the number of image data
        files read in by this method.

        Parameters
        ----------

        dataPath: string
            Path to data files or directory, specified as either a local filesystem path or in a URI-like format,
            including scheme. A datapath argument may include a single '*' wildcard character in the filename.

        ext: string, optional, default "tif"
            Extension required on data files to be loaded.

        startIdx, stopIdx: nonnegative int. optional.
            Indices of the first and last-plus-one data file to load, relative to the sorted filenames matching
            `datapath` and `ext`. Interpreted according to python slice indexing conventions.

        recursive: boolean, default False
            If true, will recursively descend directories rooted at datapath, loading all files in the tree that
            have an extension matching 'ext'. Recursive loading is currently only implemented for local filesystems
            (not s3).

        nplanes: positive integer, default None
            If passed, will cause a single multipage tif file to be subdivided into multiple time points. Every
            `nplanes` tif pages in the file will be considered as a new time point. With nplanes=None (the default), a
            single file will be considered to represent a single time point.
        """

        try:
            from PIL import Image
        except ImportError, e:
            Image = None
            raise ImportError("fromMultipageTif requires a successful 'from PIL import Image'; " +
                              "the PIL/pillow library appears to be missing or broken.", e)
        # we know that that array(pilimg) works correctly for pillow == 2.3.0, and that it
        # does not work (at least not with spark) for old PIL == 1.1.7. we believe but have not confirmed
        # that array(pilimg) works correctly for every version of pillow. thus currently we check only whether
        # our PIL library is in fact pillow, and choose our conversion function accordingly
        isPillow = hasattr(Image, "PILLOW_VERSION")
        if isPillow:
            conversionFcn = array  # use numpy's array() function
        else:
            from thunder.utils.common import pil_to_array
            conversionFcn = pil_to_array  # use our modified version of matplotlib's pil_to_array

        if nplanes is not None and nplanes <= 0:
            raise ValueError("nplanes must be positive if passed, got %d" % nplanes)

        def multitifReader(idxAndBuf):
            idx, buf = idxAndBuf
            fbuf = BytesIO(buf)
            multipage = Image.open(fbuf)
            pageIdx = 0
            imgArys = []
            npagesLeft = -1 if nplanes is None else nplanes  # counts number of planes remaining in image if positive
            values = []
            while True:
                try:
                    multipage.seek(pageIdx)
                    imgArys.append(conversionFcn(multipage))
                    pageIdx += 1
                    npagesLeft -= 1
                    if npagesLeft == 0:
                        # we have just finished an image from this file
                        retAry = dstack(imgArys) if len(imgArys) > 1 else imgArys[0]
                        values.append(retAry)
                        # reset counters:
                        npagesLeft = nplanes
                        imgArys = []
                except EOFError:
                    # past last page in tif
                    break
            if imgArys:
                retAry = dstack(imgArys) if len(imgArys) > 1 else imgArys[0]
                values.append(retAry)
            nvals = len(values)
            keys = [idx*nvals + timepoint for timepoint in xrange(nvals)]
            return zip(keys, values)

        reader = getParallelReaderForPath(dataPath)(self.sc)
        readerRdd = reader.read(dataPath, ext=ext, startIdx=startIdx, stopIdx=stopIdx, recursive=recursive)
        nimages = reader.lastNRecs if nplanes is None else None
        return Images(readerRdd.flatMap(multitifReader), nimages=nimages)

    def fromPng(self, dataPath, ext='png', startIdx=None, stopIdx=None, recursive=False):
        """Load an Images object stored in a directory of png files

        The RDD wrapped by the returned Images object will have a number of partitions equal to the number of image data
        files read in by this method.

        Parameters
        ----------

        dataPath: string
            Path to data files or directory, specified as either a local filesystem path or in a URI-like format,
            including scheme. A datapath argument may include a single '*' wildcard character in the filename.

        ext: string, optional, default "png"
            Extension required on data files to be loaded.

        startIdx, stopIdx: nonnegative int. optional.
            Indices of the first and last-plus-one data file to load, relative to the sorted filenames matching
            `datapath` and `ext`. Interpreted according to python slice indexing conventions.

        recursive: boolean, default False
            If true, will recursively descend directories rooted at datapath, loading all files in the tree that
            have an extension matching 'ext'. Recursive loading is currently only implemented for local filesystems
            (not s3).
        """
        def readPngFromBuf(buf):
            fbuf = BytesIO(buf)
            return imread(fbuf, format='png')

        reader = getParallelReaderForPath(dataPath)(self.sc)
        readerRdd = reader.read(dataPath, ext=ext, startIdx=startIdx, stopIdx=stopIdx, recursive=recursive)
        return Images(readerRdd.mapValues(readPngFromBuf), nimages=reader.lastNRecs)
