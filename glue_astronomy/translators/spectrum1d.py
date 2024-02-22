import numpy as np
import warnings

from glue.config import data_translator
from glue.core import Data, Subset

from gwcs import WCS as GWCS

from astropy import units as u
from astropy.coordinates import SpectralCoord
from astropy.wcs import WCS, WCSSUB_SPECTRAL
from astropy.nddata import StdDevUncertainty, InverseVariance, VarianceUncertainty
from astropy.wcs.wcsapi.wrappers.base import BaseWCSWrapper
from astropy.wcs.wcsapi import HighLevelWCSMixin, BaseHighLevelWCS

from glue_astronomy.spectral_coordinates import SpectralCoordinates

from specutils import Spectrum1D

UNCERT_REF = {'std': StdDevUncertainty,
              'var': VarianceUncertainty,
              'ivar': InverseVariance}


UCD_TO_SPECTRAL_NAME = {'em.freq': 'Frequency',
                        'em.energy': 'Energy',
                        'em.wavenumber': 'Wavenumber',
                        'em.wl': 'Wavelength',
                        'spect.dopplerVeloc.radio': 'Velocity',
                        'spect.dopplerVeloc.opt': 'Velocity',
                        'spect.dopplerVeloc': 'Velocity',
                        'src.redshift': 'Redshift',
                        'custom:spect.doplerVeloc.beta': 'Beta'}


class PaddedSpectrumWCS(BaseWCSWrapper, HighLevelWCSMixin):

    # Spectrum1D can use a 1D spectral WCS even for n-dimensional
    # datasets while glue always needs the dimensionality to match,
    # so this class pads the WCS so that it is n-dimensional.

    # NOTE: This class could be updated to use CompoundLowLevelWCS from NDCube.

    def __init__(self, wcs, ndim, spectral_axis_index):
        self.spectral_wcs = wcs
        self.flux_ndim = ndim
        self.spectral_axis_index = spectral_axis_index

        if self.flux_ndim == 2:
            self.spatial_keys = ['spatial']
        else:
            self.spatial_keys = [f"spatial{i}" for i in range(0, self.flux_ndim-1)]

    @property
    def pixel_n_dim(self):
        return self.flux_ndim

    @property
    def world_n_dim(self):
        return self.flux_ndim

    @property
    def world_axis_physical_types(self):
        return [self.spectral_wcs.world_axis_physical_types[0], *[None]*(self.flux_ndim-1)]

    @property
    def world_axis_units(self):
        return (self.spectral_wcs.world_axis_units[0], *[None]*(self.flux_ndim-1))

    def pixel_to_world_values(self, *pixel_arrays):
        # The ravel and reshape are needed because of
        # https://github.com/astropy/astropy/issues/12154
        px = np.array(pixel_arrays[0])
        world_arrays = [self.spectral_wcs.pixel_to_world_values(px.ravel()).reshape(px.shape),
                        *pixel_arrays[1:]]
        return tuple(world_arrays)

    def world_to_pixel_values(self, *world_arrays):
        # The ravel and reshape are needed because of
        # https://github.com/astropy/astropy/issues/12154
        wx = np.array(world_arrays[0])
        pixel_arrays = [self.spectral_wcs.world_to_pixel_values(wx.ravel()).reshape(wx.shape),
                        *world_arrays[1:]]
        return tuple(pixel_arrays)

    @property
    def world_axis_object_components(self):
        return [self.spectral_wcs.world_axis_object_components[0],
                *[(key, 'value', 'value') for key in self.spatial_keys]]

    @property
    def world_axis_object_classes(self):
        spectral_key = self.spectral_wcs.world_axis_object_components[0][0]
        obj_classes = {spectral_key: self.spectral_wcs.world_axis_object_classes[spectral_key]}
        for key in self.spatial_keys:
            obj_classes[key] = (u.Quantity, (), {'unit': u.pixel})

        return obj_classes

    @property
    def pixel_shape(self):
        return None

    @property
    def pixel_bounds(self):
        return None

    @property
    def pixel_axis_names(self):
        return tuple([self.spectral_wcs.pixel_axis_names[0], *self.spatial_keys])

    @property
    def world_axis_names(self):
        if self.flux_ndim == 2:
            names = ['Offset']
        else:
            names = [f"Offset{i}" for i in range(0, self.flux_ndim-1)]

        return (UCD_TO_SPECTRAL_NAME.get(self.spectral_wcs.world_axis_physical_types[0], ''),
                *names)

    @property
    def axis_correlation_matrix(self):
        return np.identity(self.flux_ndim).astype('bool')

    @property
    def serialized_classes(self):
        return False


@data_translator(Spectrum1D)
class SpecutilsHandler:

    def _has_homogenous_spectral_solution(self, data):
        # Check to see if a GWCS gives the same spectral solution at every spatial point
        if isinstance(data, Spectrum1D):
            spectral_axis_index = data.spectral_axis_index
            data_ndim = data.flux.ndim
        else:
            spectral_axis_index = data.meta['spectral_axis_index']
            data_ndim = data.ndim

        axes = tuple(i for i in range(data_ndim) if i != spectral_axis_index)

        corners = list(np.zeros((data_ndim, 4)))
        for i in axes:
            corners[i][1] = data.shape[i]-1
            corners[i][3] = data.shape[i]-1
        corners[spectral_axis_index][2] = data.shape[spectral_axis_index]-1
        corners[spectral_axis_index][3] = data.shape[spectral_axis_index]-1
        # WCS order vs array order
        corners.reverse()

        test_world = data.coords.pixel_to_world(*corners)
        spec_coord = [x for x in test_world if isinstance(x, SpectralCoord)][0]

        return np.isclose(spec_coord[0], spec_coord[1]) and np.isclose(spec_coord[2], spec_coord[3])  # noqa

    def to_data(self, obj):

        # specutils 2.0 doesn't care where the spectral axis anymore, but we still need
        # PaddedSpectrumWCS for now
        if obj.flux.ndim > 1 and obj.wcs.world_n_dim == 1:
            data = Data(coords=PaddedSpectrumWCS(obj.wcs, obj.flux.ndim, obj.spectral_axis_index))
        else:
            data = Data(coords=obj.wcs)

        data['flux'] = obj.flux
        data.get_component('flux').units = str(obj.unit)

        # Include uncertainties if they exist
        if obj.uncertainty is not None:
            data['uncertainty'] = obj.uncertainty.quantity
            data.get_component('uncertainty').units = str(obj.uncertainty.unit)
            data.meta.update({'uncertainty_type': obj.uncertainty.uncertainty_type})

        # Include mask if it exists
        if obj.mask is not None:
            data['mask'] = obj.mask

        # Log which is the spectral axis
        data.meta.update({'spectral_axis_index': obj.spectral_axis_index})

        data.meta.update(obj.meta)

        return data

    def to_object(self, data_or_subset, attribute=None, statistic='mean'):
        """
        Convert a glue Data object to a Spectrum1D object.

        Parameters
        ----------
        data_or_subset : `glue.core.data.Data` or `glue.core.subset.Subset`
            The data to convert to a Spectrum1D object
        attribute : `glue.core.component_id.ComponentID`
            The attribute to use for the Spectrum1D data
        statistic : {'minimum', 'maximum', 'mean', 'median', 'sum', 'percentile'}
            The statistic to use to collapse the dataset
        """

        if isinstance(data_or_subset, Subset):
            data = data_or_subset.data
            subset_state = data_or_subset.subset_state
        else:
            data = data_or_subset
            subset_state = None

        if data.ndim < 2 and statistic is not None:
            statistic = None

        if statistic is None and isinstance(data.coords, BaseHighLevelWCS):

            if isinstance(data.coords, PaddedSpectrumWCS):
                kwargs = {'wcs': data.coords.spectral_wcs}
            else:
                kwargs = {'wcs': data.coords}

        elif statistic is not None:

            spectral_axis_index = data.meta['spectral_axis_index']
            axes = tuple(i for i in range(data.ndim) if i != spectral_axis_index)
            if isinstance(data.coords, PaddedSpectrumWCS):
                kwargs = {'wcs': data.coords.spectral_wcs}
            elif isinstance(data.coords, WCS):
                kwargs = {'wcs': data.coords.sub([WCSSUB_SPECTRAL])}
            elif isinstance(data.coords, GWCS):
                # Check if we need to resample to a common spectral axis for all spatial
                # points before collapsing or if all spaxels have same solution
                if self._has_homogenous_spectral_solution(data):
                    wcs_args = []
                    for i in range(len(data.shape)):
                        wcs_args.append(np.zeros(data.shape[spectral_axis_index]))
                    wcs_args[spectral_axis_index] = np.arange(data.shape[spectral_axis_index])
                    wcs_args.reverse()
                    spectral_and_spatial = data.coords.pixel_to_world(*wcs_args)
                    spectral_axis = [x for x in spectral_and_spatial if isinstance(x, SpectralCoord)][0]  # noqa
                    kwargs = {'spectral_axis': spectral_axis}

                else:
                    # In this case the flux should be resampled onto a common spectral axis
                    # before collapsing
                    warnings.warn('Spectral solution is not the same at all spatial points,'
                                  ' collapsing may give inaccurate results.')
                    kwargs = {'wcs': data.coords}
            else:
                # Shouldn't get here anymore, but just in case.
                raise ValueError('Can only use statistic= if the Data object has a GWCS'
                                 ' or FITS WCS')

        elif isinstance(data.coords, SpectralCoordinates):

            kwargs = {'spectral_axis': data.coords.spectral_axis}

        else:
            raise TypeError('data.coords should be an instance of WCS '
                            'or SpectralCoordinates')

        # Copy over metadata
        kwargs['meta'] = data.meta.copy()

        if isinstance(attribute, str):
            attribute = data.id[attribute]
        elif len(data.main_components) == 0:
            raise ValueError('Data object has no attributes.')
        elif attribute is None:
            if len(data.main_components) == 1:
                attribute = data.main_components[0]
            # If no specific attribute is defined, attempt to retrieve
            # the flux and uncertainty, if available
            elif any([x.label in ('flux', 'uncertainty') for x in data.components]):
                attribute = [data.find_component_id(x)
                             for x in ('flux', 'uncertainty')
                             if data.find_component_id(x) is not None]
            else:
                raise ValueError("Data object has more than one attribute, so "
                                 "you will need to specify which one to use as "
                                 "the flux for the spectrum using the "
                                 "attribute= keyword argument.")

        def parse_attributes(attributes):
            data_kwargs = {}

            for attribute in attributes:
                component = data.get_component(attribute)

                # Get mask if there is one defined, or if this is a subset
                if subset_state is None:
                    mask = None
                else:
                    mask = data.get_mask(subset_state=subset_state)
                    mask = ~mask

                # Collapse values and mask to profile
                if data.ndim > 1 and statistic is not None:
                    # Get units and attach to value
                    values = data.compute_statistic(statistic, attribute, axis=axes,
                                                    subset_state=subset_state)
                    if mask is not None:
                        collapse_axes = tuple([x for x in range(0, data.ndim) if
                                               x != data.meta['spectral_axis_index']])
                        mask = np.all(mask, collapse_axes)
                else:
                    values = data.get_data(attribute)

                attribute_label = attribute.label

                if attribute_label not in ('flux', 'uncertainty'):
                    attribute_label = 'flux'

                values = u.Quantity(values, unit=component.units)

                # If the attribute is uncertainty, we must coerce it to a
                #  specific uncertainty type. If no value exists in the glue
                #  object meta dictionary, use standard deviation.
                if attribute_label == 'uncertainty':
                    values = UNCERT_REF[
                        data.meta.get('uncertainty_type', 'std')](values)

                data_kwargs.update({attribute_label: values,
                                   'mask': mask})

            return data_kwargs

        data_kwargs = parse_attributes(
            [attribute] if not hasattr(attribute, '__len__') else attribute)

        return Spectrum1D(**data_kwargs, **kwargs)
