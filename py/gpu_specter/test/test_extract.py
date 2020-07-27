import unittest, os, shutil, uuid
import pkg_resources
from astropy.table import Table
import numpy as np

from gpu_specter.io import read_psf
from gpu_specter.extract.cpu import projection_matrix, get_spots, ex2d_patch, get_resolution_diags
from gpu_specter.extract.both import xp_ex2d_patch

try:
    import specter.psf
    import specter.extract
    specter_available = True
except ImportError:
    specter_available = False

try:
    import cupy as cp
    cupy_available = cp.is_available()
except ImportError:
    cupy_available = False

class TestEx2dPatch(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.psffile = pkg_resources.resource_filename(
            'gpu_specter', 'test/data/psf-r0-00051060.fits')
        cls.psfdata = read_psf(cls.psffile)

        cls.wavelengths = np.arange(6000, 6050, 1)
        nwave = len(cls.wavelengths)
        nspec = 5

        spots, corners = get_spots(0, nspec, cls.wavelengths, cls.psfdata)
        cls.A4, cls.xyrange = projection_matrix(0, nspec, 0, nwave, spots, corners)

        phot = np.zeros((nspec, nwave))
        phot[0] = 100
        phot[1] = 5*np.arange(nwave)
        phot[2] = 50
        phot[4] = 100*(1+np.sin(np.arange(nwave)/10.))
        phot[0,10] += 500
        phot[1,15] += 200
        phot[2,20] += 300
        phot[3,25] += 1000
        phot[4,30] += 600

        cls.phot = phot

        xmin, xmax, ymin, ymax = cls.xyrange
        ny = ymax - ymin
        nx = xmax - xmin

        A2 = cls.A4.reshape(ny*nx, nspec*nwave)
        cls.img = A2.dot(cls.phot.ravel()).reshape(ny, nx)

        cls.readnoise = 3.0
        cls.noisyimg = np.random.normal(loc=0.0, scale=cls.readnoise, size=(ny, nx))
        cls.noisyimg += np.random.poisson(cls.img)
        #- for test, cheat by using noiseless img instead of noisyimg to estimate variance
        cls.imgivar = 1.0/(cls.img + cls.readnoise**2)

    @classmethod
    def tearDownClass(cls):
        pass

    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_basics(self):
        ny, nx, nspec, nwave = self.A4.shape

        img = np.random.randn(ny, nx)
        imgivar = np.ones((ny, nx))

        flux, varflux, R = ex2d_patch(img, imgivar, self.A4)

        self.assertEqual(flux.shape, (nspec, nwave))
        self.assertEqual(varflux.shape, (nspec, nwave))
        self.assertEqual(R.shape, (nspec*nwave, nspec*nwave))

    def test_compare_xp_cpu(self):
        # Compare the "signal" decorrelation method
        flux0, ivar0, R0 = ex2d_patch(self.noisyimg, self.imgivar, self.A4, decorrelate='signal')
        flux1, ivar1, R1 = xp_ex2d_patch(self.noisyimg, self.imgivar, self.A4, decorrelate='signal')

        self.assertTrue(np.allclose(flux0, flux1))
        self.assertTrue(np.allclose(ivar0, ivar1))
        self.assertTrue(np.allclose(R0, R1))
        self.assertTrue(np.allclose(np.abs(flux0 - flux1)/np.sqrt(1./ivar0 + 1./ivar1), np.zeros_like(flux0)))

        # Compare the "noise" decorrelation method
        flux0, ivar0, R0 = ex2d_patch(self.noisyimg, self.imgivar, self.A4, decorrelate='noise')
        flux1, ivar1, R1 = xp_ex2d_patch(self.noisyimg, self.imgivar, self.A4, decorrelate='noise')

        self.assertTrue(np.allclose(flux0, flux1))
        self.assertTrue(np.allclose(ivar0, ivar1))
        self.assertTrue(np.allclose(R0, R1))
        self.assertTrue(np.allclose(np.abs(flux0 - flux1)/np.sqrt(1./ivar0 + 1./ivar1), np.zeros_like(flux0)))

    @unittest.skipIf(not cupy_available, 'cupy not available')
    def test_compare_icov(self):
        from gpu_specter.extract.cpu import dotdot1, dotdot2, dotdot3

        ny, nx, nspec, nwave = self.A4.shape

        pixel_ivar = self.imgivar.ravel()
        A = self.A4.reshape(ny*nx, nspec*nwave)

        icov0 = A.T.dot(np.diag(pixel_ivar).dot(A))
        icov1 = dotdot1(A, pixel_ivar) # array broadcast
        icov2 = dotdot2(A, pixel_ivar) # scipy sparse
        icov3 = dotdot3(A, pixel_ivar) # numba

        pixel_ivar_gpu = cp.asarray(pixel_ivar)
        A_gpu = cp.asarray(A)
        icov_gpu = (A_gpu.T * pixel_ivar_gpu).dot(A_gpu) # array broadcast

        eps_double = np.finfo(np.float64).eps
        np.testing.assert_allclose(icov0, icov1, rtol=2*eps_double, atol=0)
        np.testing.assert_allclose(icov0, icov2, rtol=10*eps_double, atol=0)
        np.testing.assert_allclose(icov0, icov3, rtol=10*eps_double, atol=0)

        np.testing.assert_allclose(icov0, cp.asnumpy(icov_gpu), rtol=10*eps_double, atol=0)
        np.testing.assert_allclose(icov1, cp.asnumpy(icov_gpu), rtol=10*eps_double, atol=0)
        np.testing.assert_allclose(icov2, cp.asnumpy(icov_gpu), rtol=10*eps_double, atol=0)
        np.testing.assert_allclose(icov3, cp.asnumpy(icov_gpu), rtol=10*eps_double, atol=0)

    @unittest.skipIf(not cupy_available, 'cupy not available')
    def test_compare_solve(self):
        import scipy.linalg

        ny, nx, nspec, nwave = self.A4.shape

        pixel_values = self.noisyimg.ravel()
        pixel_ivar = self.imgivar.ravel()
        A = self.A4.reshape(ny*nx, nspec*nwave)

        icov = (A.T * pixel_ivar).dot(A)
        y = (A.T * pixel_ivar).dot(pixel_values)
        deconvolved_scipy = scipy.linalg.solve(icov, y)
        deconvolved_numpy = np.linalg.solve(icov, y)

        icov_gpu = cp.asarray(icov)
        y_gpu = cp.asarray(y)

        deconvolved_gpu = cp.linalg.solve(icov_gpu, y_gpu)

        eps_double = np.finfo(np.float64).eps
        np.testing.assert_allclose(deconvolved_scipy, deconvolved_numpy, rtol=eps_double, atol=0)
        np.testing.assert_allclose(deconvolved_scipy, cp.asnumpy(deconvolved_gpu), rtol=1e5*eps_double, atol=0)
        np.testing.assert_allclose(deconvolved_numpy, cp.asnumpy(deconvolved_gpu), rtol=1e5*eps_double, atol=0)


    @unittest.skipIf(not cupy_available, 'cupy not available')
    def test_compare_deconvolve(self):

        from gpu_specter.extract.cpu import deconvolve as cpu_deconvolve
        from gpu_specter.extract.both import xp_deconvolve as gpu_deconvolve

        ny, nx, nspec, nwave = self.A4.shape

        pixel_values = self.noisyimg.ravel()
        pixel_ivar = self.imgivar.ravel()
        A = self.A4.reshape(ny*nx, nspec*nwave)

        pixel_values_gpu = cp.asarray(pixel_values)
        pixel_ivar_gpu = cp.asarray(pixel_ivar)
        A_gpu = cp.asarray(A)

        deconvolved0, iCov0 = cpu_deconvolve(pixel_values, pixel_ivar, A)
        deconvolved_gpu, iCov_gpu = gpu_deconvolve(pixel_values_gpu, pixel_ivar_gpu, A_gpu)

        deconvolved1 = cp.asnumpy(deconvolved_gpu)
        iCov1 = cp.asnumpy(iCov_gpu)

        eps_double = np.finfo(np.float64).eps
        np.testing.assert_allclose(deconvolved0, deconvolved1, rtol=1e5*eps_double, atol=0)
        np.testing.assert_allclose(iCov0, iCov1, rtol=1e3*eps_double, atol=0)


    @unittest.skipIf(not cupy_available, 'cupy not available')
    def test_compare_get_Rdiags(self):
        nspec, ispec, specmin = 5, 5, 4
        nwave, wavepad, ndiag = 50, 10, 7
        nwavetot = nwave + 2*wavepad
        n = nwavetot*(2+nspec)
        R = np.arange(n*n).reshape(n, n)

        Rdiags0 = get_resolution_diags(R, ndiag, ispec-specmin, nspec, nwave, wavepad)

        R_gpu = cp.asarray(R)
        Rdiags1_gpu = gpu_get_resolution_diags(R_gpu, ndiag, ispec-specmin, nspec, nwave, wavepad)

        self.assertTrue(np.alltrue(Rdiags0 == cp.asnumpy(Rdiags1_gpu)))


    @unittest.skipIf(not cupy_available, 'cupy not available')
    def test_compare_xp_gpu(self):
        noisyimg_gpu = cp.asarray(self.noisyimg)
        imgivar_gpu = cp.asarray(self.imgivar)
        A4_gpu = cp.asarray(self.A4)

        # Compare the "signal" decorrelation method
        flux0, ivar0, R0 = ex2d_patch(self.noisyimg, self.imgivar, self.A4, decorrelate='signal')
        flux1_gpu, ivar1_gpu, R1_gpu = xp_ex2d_patch(noisyimg_gpu, imgivar_gpu, A4_gpu, decorrelate='signal')

        flux1 = cp.asnumpy(flux1_gpu)
        ivar1 = cp.asnumpy(ivar1_gpu)
        R1 = cp.asnumpy(R1_gpu)

        eps_double = np.finfo(np.float64).eps

        self.assertTrue(np.allclose(flux0, flux1, rtol=1e5*eps_double, atol=0))
        self.assertTrue(np.allclose(ivar0, ivar1, rtol=1e3*eps_double, atol=0))
        self.assertTrue(np.allclose(np.diag(R0), np.diag(R1), rtol=1e2*eps_double, atol=0))
        self.assertTrue(np.allclose(np.abs(flux0 - flux1)/np.sqrt(1./ivar0 + 1./ivar1), np.zeros_like(flux0)))

        # Compare the "noise" decorrelation method
        flux0, ivar0, R0 = ex2d_patch(self.noisyimg, self.imgivar, self.A4, decorrelate='noise')
        flux1_gpu, ivar1_gpu, R1_gpu = xp_ex2d_patch(noisyimg_gpu, imgivar_gpu, A4_gpu, decorrelate='noise')

        flux1 = cp.asnumpy(flux1_gpu)
        ivar1 = cp.asnumpy(ivar1_gpu)
        R1 = cp.asnumpy(R1_gpu)

        self.assertTrue(np.allclose(flux0, flux1, rtol=1e5*eps_double, atol=0))
        self.assertTrue(np.allclose(ivar0, ivar1, rtol=1e3*eps_double, atol=0))
        self.assertTrue(np.allclose(np.diag(R0), np.diag(R1), rtol=1e2*eps_double, atol=0))
        self.assertTrue(np.allclose(np.abs(flux0 - flux1)/np.sqrt(1./ivar0 + 1./ivar1), np.zeros_like(flux0)))

    @unittest.skipIf(not specter_available, 'specter not available')
    def test_compare_specter(self):
        ny, nx, nspec, nwave = self.A4.shape

        psf = specter.psf.load_psf(self.psffile)
        img = psf.project(self.wavelengths, self.phot, xyrange=self.xyrange)

        # self.assertTrue(np.allclose(self.img, img))

        #- Compare the "signal" decorrelation method
        flux0, ivar0, R0 = specter.extract.ex2d_patch(self.noisyimg, self.imgivar, psf, 0, nspec,
            self.wavelengths, xyrange=self.xyrange, ndecorr=False)
        flux1, ivar1, R1 = ex2d_patch(self.noisyimg, self.imgivar, self.A4, decorrelate='signal')

        #- Note that specter is using it's version of the projection matrix
        # A = psf.projection_matrix((0, nspec), self.wavelengths, self.xyrange).toarray()
        # A4 = A.reshape(self.A4.shape)
        # flux1, ivar1, R1 = ex2d_patch(self.noisyimg, self.imgivar, A4, decorrelate='signal')

        self.assertTrue(np.allclose(flux0, flux1))
        self.assertTrue(np.allclose(ivar0, ivar1))
        self.assertTrue(np.allclose(R0, R1))
        #self.assertTrue(np.allclose(np.abs(flux0 - flux1)/np.sqrt(1./ivar0 + 1./ivar1), np.zeros_like(flux0)))

        # Compare the "noise" decorrelation method
        flux0, ivar0, R0 = specter.extract.ex2d_patch(self.noisyimg, self.imgivar, psf, 0, nspec,
            self.wavelengths, xyrange=self.xyrange, ndecorr=True)
        flux1, ivar1, R1 = ex2d_patch(self.noisyimg, self.imgivar, self.A4, decorrelate='noise')

        self.assertTrue(np.allclose(flux0, flux1))
        self.assertTrue(np.allclose(ivar0, ivar1))
        self.assertTrue(np.allclose(R0, R1))
        #self.assertTrue(np.allclose(np.abs(flux0 - flux1)/np.sqrt(1./ivar0 + 1./ivar1), np.zeros_like(flux0)))


if __name__ == '__main__':
    unittest.main()
