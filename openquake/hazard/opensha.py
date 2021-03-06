# -*- coding: utf-8 -*-

# Copyright (c) 2010-2011, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version 3
# only, as published by the Free Software Foundation.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License version 3 for more details
# (a copy is included in the LICENSE file that accompanied this code).
#
# You should have received a copy of the GNU Lesser General Public License
# version 3 along with OpenQuake.  If not, see
# <http://www.gnu.org/licenses/lgpl-3.0.txt> for a copy of the LGPLv3 License.

"""
Wrapper around the OpenSHA-lite java library.
"""

import math
import os
import multiprocessing
import numpy
import random

from db.alchemy.db_utils import get_uiapi_writer_session

from openquake import java
from openquake import kvs
from openquake import logs
from openquake import settings
from openquake import shapes
from openquake import xml

from openquake.hazard import classical_psha
from openquake.hazard import job
from openquake.hazard import tasks
from openquake.job.mixins import Mixin
from openquake.kvs import tokens
from openquake.output import geotiff, cpt
from openquake.output import hazard as hazard_output
from openquake.utils import tasks as utils_tasks

LOG = logs.LOG

# NOTE: this refers to how the values are stored in KVS. In the config
# file, values are stored untransformed (i.e., the list of IMLs is
# not stored as logarithms).
IML_SCALING = {'PGA': numpy.log,
               'MMI': lambda iml: iml,
               'PGV': numpy.log,
               'PGD': numpy.log,
               'SA': numpy.log,
              }

HAZARD_CURVE_FILENAME_PREFIX = 'hazardcurve'
HAZARD_MAP_FILENAME_PREFIX = 'hazardmap'


def preload(fn):
    """A decorator for preload steps that must run on the Jobber node"""
    def preloader(self, *args, **kwargs):
        """Validate job"""
        self.cache = java.jclass("KVS")(
                settings.KVS_HOST,
                settings.KVS_PORT)
        self.calc = java.jclass("LogicTreeProcessor")(
                self.cache, self.key)
        java.jvm().java.lang.System.setProperty("openquake.nrml.schema",
                                                xml.nrml_schema_file())
        return fn(self, *args, **kwargs)
    return preloader


def unwrap_validation_error(jpype, runtime_exception, path=None):
    """Unwraps the nested exception of a runtime exception.  Throws
    either a XMLValidationError or the original Java exception"""
    ex = runtime_exception.__javaobject__

    if ex.getCause() and type(ex.getCause()) is \
            jpype.JPackage('org').dom4j.DocumentException:
        if path:
            msg = '%s: %s' % (path, ex.getCause().getMessage())
        else:
            msg = ex.getCause().getMessage()
        raise xml.XMLValidationError(msg)

    raise runtime_exception


class BasePSHAMixin(Mixin):
    """Contains common functionality for PSHA Mixins."""

    def store_source_model(self, seed):
        """Generates an Earthquake Rupture Forecast, using the source zones and
        logic trees specified in the job config file. Note that this has to be
        done currently using the file itself, since it has nested references to
        other files."""

        LOG.info("Storing source model from job config")
        key = kvs.generate_product_key(self.id, kvs.tokens.SOURCE_MODEL_TOKEN)
        print "source model key is", key
        jpype = java.jvm()
        try:
            self.calc.sampleAndSaveERFTree(self.cache, key, seed)
        except jpype.JException(jpype.java.lang.RuntimeException), ex:
            unwrap_validation_error(
                jpype, ex,
                self.params.get("SOURCE_MODEL_LOGIC_TREE_FILE_PATH"))

    def store_gmpe_map(self, seed):
        """Generates a hash of tectonic regions and GMPEs, using the logic tree
        specified in the job config file."""
        key = kvs.generate_product_key(self.id, kvs.tokens.GMPE_TOKEN)
        print "GMPE map key is", key
        jpype = java.jvm()
        try:
            self.calc.sampleAndSaveGMPETree(self.cache, key, seed)
        except jpype.JException(jpype.java.lang.RuntimeException), ex:
            unwrap_validation_error(
                jpype, ex, self.params.get("GMPE_LOGIC_TREE_FILE_PATH"))

    def generate_erf(self):
        """Generate the Earthquake Rupture Forecast from the currently stored
        source model logic tree."""
        key = kvs.generate_product_key(self.id, kvs.tokens.SOURCE_MODEL_TOKEN)
        sources = java.jclass("JsonSerializer").getSourceListFromCache(
                    self.cache, key)
        erf = java.jclass("GEM1ERF")(sources)
        self.calc.setGEM1ERFParams(erf)
        return erf

    def set_gmpe_params(self, gmpe_map):
        """Push parameters from configuration file into the GMPE objects"""
        jpype = java.jvm()
        gmpe_lt_data = self.calc.createGmpeLogicTreeData()
        for tect_region in gmpe_map.keySet():
            gmpe = gmpe_map.get(tect_region)
            gmpe_lt_data.setGmpeParams(self.params['COMPONENT'],
                self.params['INTENSITY_MEASURE_TYPE'],
                jpype.JDouble(float(self.params['PERIOD'])),
                jpype.JDouble(float(self.params['DAMPING'])),
                self.params['GMPE_TRUNCATION_TYPE'],
                jpype.JDouble(float(self.params['TRUNCATION_LEVEL'])),
                self.params['STANDARD_DEVIATION_TYPE'],
                jpype.JDouble(float(self.params['REFERENCE_VS30_VALUE'])),
                jpype.JObject(gmpe, java.jclass("AttenuationRelationship")))
            gmpe_map.put(tect_region, gmpe)

    def generate_gmpe_map(self):
        """Generate the GMPE map from the stored GMPE logic tree."""
        key = kvs.generate_product_key(self.id, kvs.tokens.GMPE_TOKEN)
        gmpe_map = java.jclass(
            "JsonSerializer").getGmpeMapFromCache(self.cache, key)
        self.set_gmpe_params(gmpe_map)
        return gmpe_map

    def get_iml_list(self):
        """Build the appropriate Arbitrary Discretized Func from the IMLs,
        based on the IMT"""

        iml_list = java.jclass("ArrayList")()
        for val in self.params['INTENSITY_MEASURE_LEVELS'].split(","):
            iml_list.add(
                IML_SCALING[self.params['INTENSITY_MEASURE_TYPE']](
                float(val)))
        return iml_list

    def parameterize_sites(self, site_list):
        """Convert python Sites to Java Sites, and add default parameters."""
        # TODO(JMC): There's Java code for this already, sets each site to have
        # the same default parameters

        jpype = java.jvm()
        jsite_list = java.jclass("ArrayList")()
        for x in site_list:
            site = x.to_java()

            vs30 = java.jclass("DoubleParameter")(jpype.JString("Vs30"))
            vs30.setValue(float(self.params['REFERENCE_VS30_VALUE']))
            depth25 = java.jclass("DoubleParameter")("Depth 2.5 km/sec")
            depth25.setValue(float(
                    self.params['REFERENCE_DEPTH_TO_2PT5KM_PER_SEC_PARAM']))
            sadigh = java.jclass("StringParameter")("Sadigh Site Type")
            sadigh.setValue(self.params['SADIGH_SITE_TYPE'])
            site.addParameter(vs30)
            site.addParameter(depth25)
            site.addParameter(sadigh)
            jsite_list.add(site)
        return jsite_list


class ClassicalMixin(BasePSHAMixin):
    """Classical PSHA method for performing Hazard calculations.

    Implements the JobMixin, which has a primary entry point of execute().
    Execute is responsible for dispatching celery tasks.

    Note that this Mixin, during execution, will always be an instance of the
    Job class, and thus has access to the self.params dict, full of config
    params loaded from the Job configuration file."""

    def number_of_tasks(self):
        """How many `celery` tasks should be used for the calculations?"""
        value = self.params.get("HAZARD_TASKS")
        value = value.strip() if value else None
        return 2 * multiprocessing.cpu_count() if value is None else int(value)

    def do_curves(self, sites, serializer=None,
                  the_task=tasks.compute_hazard_curve):
        """Trigger the calculation of hazard curves, serialize as requested.

        The calculated curves will only be serialized if the `serializer`
        parameter is not `None`.

        :param sites: The sites for which to calculate hazard curves.
        :type sites: list of :py:class:`openquake.shapes.Site`
        :param serializer: A serializer for the calculated hazard curves,
            receives the KVS keys of the calculated hazard curves in
            its single parameter.
        :type serializer: a callable with a single parameter: list of strings
        :param the_task: The `celery` task to use for the hazard curve
            calculation, it takes the following parameters:
                * job ID
                * the sites for which to calculate the hazard curves
                * the logic tree realization number
        :type the_task: a callable taking three parameters
        :returns: KVS keys of the calculated hazard curves.
        :rtype: list of string
        """
        results = []

        source_model_generator = random.Random()
        source_model_generator.seed(
                self.params.get("SOURCE_MODEL_LT_RANDOM_SEED", None))

        gmpe_generator = random.Random()
        gmpe_generator.seed(self.params.get("GMPE_LT_RANDOM_SEED", None))

        realizations = int(self.params["NUMBER_OF_LOGIC_TREE_SAMPLES"])

        LOG.info("Going to run classical PSHA hazard for %s realizations "
                 "and %s sites" % (realizations, len(sites)))

        for realization in xrange(0, realizations):
            LOG.info("Calculating hazard curves for realization %s"
                     % realization)
            self.store_source_model(source_model_generator.getrandbits(32))
            self.store_gmpe_map(source_model_generator.getrandbits(32))

            results_per_realization = utils_tasks.distribute(
                self.number_of_tasks(), the_task, ("site_list", sites),
                dict(job_id=self.id, realization=realization),
                flatten_results=True)

            if serializer:
                serializer(results_per_realization)
            results.extend(results_per_realization)

        return results

    def param_set(self, name):
        """Is the parameter with the given `name` set and non-empty?

        :param name: The name of the parameter that should be set and
            non-empty.
        :return: `True` if the parameter in question set and non-empty, `False`
            otherwise.
        :rtype: bool
        """
        value = self.params.get(name)
        return value is not None and value.strip()

    def do_means(self, sites, curve_serializer=None, map_serializer=None,
                 curve_task=tasks.compute_mean_curves,
                 map_func=classical_psha.compute_mean_hazard_maps):
        """Trigger the calculation of mean curves/maps, serialize as requested.

        The calculated mean curves/maps will only be serialized if the
        corresponding `serializer` parameter was set.

        :param sites: The sites for which to calculate mean curves/maps.
        :type sites: list of :py:class:`openquake.shapes.Site`
        :param curve_serializer: A serializer for the calculated curves,
            receives the KVS keys of the calculated curves in
            its single parameter.
        :type curve_serializer: function([string])
        :param map_serializer: A serializer for the calculated maps,
            receives the KVS keys of the calculated maps in its single
            parameter.
        :type map_serializer: function([string])
        :param curve_task: The `celery` task to use for the curve calculation,
            it takes the following parameters:
                * job ID
                * the sites for which to calculate the hazard curves
        :type curve_task: function(string, [:py:class:`openquake.shapes.Site`])
        :param map_func: A function that computes mean hazard maps.
        :type map_func: function(:py:class:`openquake.job.Job`)
        :returns: `None`
        """

        if not self.param_set("COMPUTE_MEAN_HAZARD_CURVE"):
            return

        # Compute and serialize the mean curves.
        LOG.info("Computing mean hazard curves")

        results = utils_tasks.distribute(
            self.number_of_tasks(), curve_task, ("sites", sites),
            dict(job_id=self.id), flatten_results=True)

        if curve_serializer:
            LOG.info("Serializing mean hazard curves")
            curve_serializer(results)

        if not self.param_set(classical_psha.POES_PARAM_NAME):
            return

        assert map_func, "No calculation function for mean hazard maps set."
        assert map_serializer, "No serializer for the mean hazard maps set."

        # Compute and serialize the mean curves.
        LOG.info("Computing/serializing mean hazard maps")
        results = map_func(self)
        LOG.debug("results = '%s'" % results)
        map_serializer(results)

    def do_quantiles(self, sites, curve_serializer=None, map_serializer=None,
                     curve_task=tasks.compute_quantile_curves,
                     map_func=classical_psha.compute_quantile_hazard_maps):
        """Trigger the calculation/serialization of quantile curves/maps.

        The calculated quantile curves/maps will only be serialized if the
        corresponding `serializer` parameter was set.

        :param sites: The sites for which to calculate quantile curves/maps.
        :type sites: list of :py:class:`openquake.shapes.Site`
        :param curve_serializer: A serializer for the calculated curves,
            receives the KVS keys of the calculated curves in
            its single parameter.
        :type curve_serializer: function([string])
        :param map_serializer: A serializer for the calculated maps,
            receives the KVS keys of the calculated maps in its single
            parameter.
        :type map_serializer: function([string])
        :param curve_task: The `celery` task to use for the curve calculation,
            it takes the following parameters:
                * job ID
                * the sites for which to calculate the hazard curves
        :type curve_task: function(string, [:py:class:`openquake.shapes.Site`])
        :param map_func: A function that computes quantile hazard maps.
        :type map_func: function(:py:class:`openquake.job.Job`)
        :returns: `None`
        """
        # compute and serialize quantile hazard curves
        LOG.info("Computing quantile hazard curves")

        results = utils_tasks.distribute(
            self.number_of_tasks(), curve_task, ("sites", sites),
            dict(job_id=self.id), flatten_results=True)

        # collect hazard curve keys per quantile value
        quantiles = _collect_curve_keys_per_quantile(results)

        LOG.info("Serializing quantile curves for %s values" % len(quantiles))
        for curves in quantiles.values():
            curve_serializer(curves)

        # compute quantile hazard maps
        if (not self.param_set(classical_psha.POES_PARAM_NAME) or
            len(quantiles) < 1):
            return

        assert map_func, "No calculation function for quantile maps set."
        assert map_serializer, "No serializer for the quantile maps set."

        LOG.info("Computing quantile hazard maps")
        results = map_func(self)
        quantiles = _collect_map_keys_per_quantile(results)

        LOG.info("Serializing quantile maps for %s values" % len(quantiles))
        for maps in quantiles.values():
            map_serializer(maps)

    @preload
    def execute(self):
        """
        Trigger the calculation and serialization of hazard curves, mean hazard
        curves/maps and quantile curves.
        """
        site_list = self.sites_for_region()
        results = self.do_curves(
            site_list, serializer=self.write_hazardcurve_file)
        self.do_means(site_list, curve_serializer=self.write_hazardcurve_file,
                      map_serializer=self.write_hazardmap_file)
        self.do_quantiles(
            site_list, curve_serializer=self.write_hazardcurve_file,
            map_serializer=self.write_hazardmap_file)

        return results

    def write_hazardcurve_file(self, curve_keys):
        """Generate a NRML file with hazard curves for a collection of
        hazard curves from KVS, identified through their KVS keys.

        curve_keys is a list of KVS keys of the hazard curves to be
        serialized.

        The hazard curve file can be written
        (1) for a set of hazard curves belonging to the same realization
            (= endBranchLabel) and a set of sites.
        (2) for a mean hazard curve at a set of sites
        (3) for a quantile hazard curve at a set of sites

        Mixing of these three cases is not allowed, i.e., all hazard curves
        from the set of curve_keys have to be either for the same realization,
        mean, or quantile.
        """

        if _is_mean_hazard_curve_key(curve_keys[0]):
            hc_attrib_update = {'statistics': 'mean'}
            filename_part = 'mean'
            curve_mode = 'mean'

        elif _is_quantile_hazard_curve_key(curve_keys[0]):

            # get quantile value from KVS key
            quantile_value = tokens.quantile_value_from_hazard_curve_key(
                curve_keys[0])
            hc_attrib_update = {'statistics': 'quantile',
                                'quantileValue': quantile_value}
            filename_part = "quantile-%.2f" % quantile_value
            curve_mode = 'quantile'

        elif _is_realization_hazard_curve_key(curve_keys[0]):
            realization_reference_str = \
                tokens.realization_value_from_hazard_curve_key(curve_keys[0])
            hc_attrib_update = {'endBranchLabel': realization_reference_str}
            filename_part = realization_reference_str
            curve_mode = 'realization'

        else:
            error_msg = "no valid hazard curve type found in KVS key"
            raise RuntimeError(error_msg)

        nrml_file = "%s-%s.xml" % (HAZARD_CURVE_FILENAME_PREFIX, filename_part)

        nrml_path = os.path.join(self['BASE_PATH'], self['OUTPUT_DIR'],
            nrml_file)
        iml_list = [float(param)
                    for param
                    in self.params['INTENSITY_MEASURE_LEVELS'].split(",")]

        LOG.debug("Generating NRML hazard curve file for mode %s, "\
            "%s hazard curves: %s" % (curve_mode, len(curve_keys), nrml_file))
        LOG.debug("IML: %s" % iml_list)

        xmlwriter = hazard_output.HazardCurveXMLWriter(nrml_path)
        hc_data = []

        for hc_key in curve_keys:

            if curve_mode == 'mean' and not _is_mean_hazard_curve_key(hc_key):
                error_msg = "non-mean hazard curve key found in mean mode"
                raise RuntimeError(error_msg)

            elif curve_mode == 'quantile':
                if not _is_quantile_hazard_curve_key(hc_key):
                    error_msg = "non-quantile hazard curve key found in "\
                                "quantile mode"
                    raise RuntimeError(error_msg)

                elif tokens.quantile_value_from_hazard_curve_key(hc_key) != \
                    quantile_value:
                    error_msg = "quantile value must be the same for all "\
                                "hazard curves in an instance file"
                    raise ValueError(error_msg)

            elif curve_mode == 'realization':
                if not _is_realization_hazard_curve_key(hc_key):
                    error_msg = "non-realization hazard curve key found in "\
                                "realization mode"
                    raise RuntimeError(error_msg)
                elif tokens.realization_value_from_hazard_curve_key(
                    hc_key) != realization_reference_str:
                    error_msg = "realization value must be the same for all "\
                                "hazard curves in an instance file"
                    raise ValueError(error_msg)

            hc = kvs.get_value_json_decoded(hc_key)

            site_obj = shapes.Site(float(hc['site_lon']),
                                   float(hc['site_lat']))

            # use hazard curve ordinate values (PoE) from KVS
            # NOTE(fab): At the moment, the IMLs are stored along with the
            # PoEs in KVS. However, we are using the IML list from config.
            # The IMLs from KVS are ignored. Note that IMLs from KVS are
            # in logarithmic form, but the ones from config are not.
            # The way of storing the HC data in KVS is not very
            # efficient, we should store the abscissae and ordinates
            # separately as lists and not make pairs of them
            curve_poe = []
            for curve_pair in hc['curve']:
                curve_poe.append(float(curve_pair['y']))

            hc_attrib = {'investigationTimeSpan':
                            self.params['INVESTIGATION_TIME'],
                         'IMLValues': iml_list,
                         'IMT': self.params['INTENSITY_MEASURE_TYPE'],
                         'PoEValues': curve_poe}

            hc_attrib.update(hc_attrib_update)
            hc_data.append((site_obj, hc_attrib))

        xmlwriter.serialize(hc_data)
        return nrml_path

    def write_hazardmap_file(self, map_keys):
        """Generate a NRML file with a hazard map for a collection of
        hazard map nodes from KVS, identified through their KVS keys.

        map_keys is a list of KVS keys of the hazard map nodes to be
        serialized.

        The hazard map file can be written
        (1) for a mean hazard map at a set of sites
        (2) for a quantile hazard map at a set of sites

        Mixing of these three cases is not allowed, i.e., all hazard maps
        from the set of curve_keys have to be either for mean, or quantile.
        """
        poe_list = [float(x) for x in
            self.params[classical_psha.POES_PARAM_NAME].split()]
        if len(poe_list) == 0:
            return None

        if _is_mean_hazmap_key(map_keys[0]):
            hm_attrib_update = {'statistics': 'mean'}
            filename_part = 'mean'
            map_mode = 'mean'

        elif _is_quantile_hazmap_key(map_keys[0]):

            # get quantile value from KVS key
            quantile_value = tokens.quantile_value_from_hazard_map_key(
                map_keys[0])
            hm_attrib_update = {'statistics': 'quantile',
                                'quantileValue': quantile_value}
            filename_part = "quantile-%.2f" % quantile_value
            map_mode = 'quantile'

        else:
            error_msg = "no valid hazard map type found in KVS key"
            raise RuntimeError(error_msg)

        files = []
        # path to the output directory
        output_path = os.path.join(self['BASE_PATH'], self['OUTPUT_DIR'])
        for poe in poe_list:

            nrml_file = "%s-%s-%s.xml" % (
                HAZARD_MAP_FILENAME_PREFIX, str(poe), filename_part)

            nrml_path = os.path.join(output_path, nrml_file)

            LOG.debug("Generating NRML hazard map file for PoE %s, mode %s, "\
                "%s nodes in hazard map: %s" % (
                poe, map_mode, len(map_keys), nrml_file))

            map_writer = create_hazardmap_writer(self.params, nrml_path)
            hm_data = []

            for hm_key in map_keys:

                if tokens.poe_value_from_hazard_map_key(hm_key) != poe:
                    continue

                elif map_mode == 'mean' and not _is_mean_hazmap_key(hm_key):
                    error_msg = "non-mean hazard map key found in mean mode"
                    raise RuntimeError(error_msg)

                elif map_mode == 'quantile':
                    if not _is_quantile_hazmap_key(hm_key):
                        error_msg = "non-quantile hazard map key found in "\
                                    "quantile mode"
                        raise RuntimeError(error_msg)

                    elif tokens.quantile_value_from_hazard_map_key(hm_key) != \
                        quantile_value:
                        error_msg = "quantile value must be the same for all "\
                                    "hazard map nodes in an instance file"
                        raise ValueError(error_msg)

                hm = kvs.get_value_json_decoded(hm_key)

                site_obj = shapes.Site(float(hm['site_lon']),
                                       float(hm['site_lat']))

                # use hazard map IML and vs30 values from KVS
                hm_attrib = {'investigationTimeSpan':
                                self.params['INVESTIGATION_TIME'],
                            'IMT': self.params['INTENSITY_MEASURE_TYPE'],
                            'IML': hm['IML'],
                            'vs30': hm['vs30'],
                            'poE': poe}

                hm_attrib.update(hm_attrib_update)
                hm_data.append((site_obj, hm_attrib))

            hm_geotiff_name = '%s-%s-%s.tiff' % (
                HAZARD_MAP_FILENAME_PREFIX, str(poe), filename_part)
            geotiff_path = os.path.join(output_path, hm_geotiff_name)

            self._write_hazard_map_geotiff(geotiff_path, hm_data)
            map_writer.serialize(hm_data)

            files.append(nrml_path)
            files.append(geotiff_path)

        return files

    def _write_hazard_map_geotiff(self, path, haz_map_data):
        """
        Write out a hazard map geotiff using the input hazard map data.

        :param path: path to output file, including file name
        :param haz_map_data: list of hazard map data to be serialized to the
            geotiff
        :type haz_map_data: list of tuples of (shapes.Site object, hazard map
            data dict)
        """
        try:
            cpt_path = os.path.join(
                    self.params['BASE_PATH'], self.params['HAZARD_MAP_CPT'])
            cpt_reader = cpt.CPTReader(cpt_path)
            colormap = cpt_reader.get_colormap()
        except (IOError, KeyError):
            LOG.info(
                "Unable to read hazard map CPT file from job config."
                " Using default colormap.")
            colormap = geotiff.HazardMapGeoTiffFile.DEFAULT_COLORMAP

        iml_min_max = None
        if 'HAZARD_MAP_IML_MIN' in self.params and \
            'HAZARD_MAP_IML_MAX' in self.params:
            iml_min_max = (
                float(self.params['HAZARD_MAP_IML_MIN']),
                float(self.params['HAZARD_MAP_IML_MAX']))
        hm_writer = geotiff.HazardMapGeoTiffFile(
            path, self.region.grid, colormap=colormap,
            iml_min_max=iml_min_max, html_wrapper=True)

        # write the hazard map and close the file
        return hm_writer.serialize(haz_map_data)

    @preload
    def compute_hazard_curve(self, site_list, realization):
        """ Compute hazard curves, write them to KVS as JSON,
        and return a list of the KVS keys for each curve. """
        jsite_list = self.parameterize_sites(site_list)
        jpype = java.jvm()
        try:
            calc = java.jclass("HazardCalculator")
            hazard_curves = calc.getHazardCurvesAsJson(
                jsite_list,
                self.generate_erf(),
                self.generate_gmpe_map(),
                self.get_iml_list(),
                float(self.params['MAXIMUM_DISTANCE']))
        except jpype.JException(jpype.java.lang.RuntimeException), ex:
            unwrap_validation_error(jpype, ex)

        # write the curves to the KVS and return a list of the keys
        kvs_client = kvs.get_client()
        curve_keys = []
        for i in xrange(0, len(hazard_curves)):
            curve = hazard_curves[i]
            site = site_list[i]
            curve_key = kvs.tokens.hazard_curve_key(
                self.id, realization, site)

            kvs_client.set(curve_key, curve)
            curve_keys.append(curve_key)
        return curve_keys


class EventBasedMixin(BasePSHAMixin):
    """Probabilistic Event Based method for performing Hazard calculations.

    Implements the JobMixin, which has a primary entry point of execute().
    Execute is responsible for dispatching celery tasks.

    Note that this Mixin, during execution, will always be an instance of the
    Job class, and thus has access to the self.params dict, full of config
    params loaded from the Job configuration file."""

    @preload
    def execute(self):
        """Main hazard processing block.

        Loops through various random realizations, spawning tasks to compute
        GMFs."""
        results = []

        source_model_generator = random.Random()
        source_model_generator.seed(
                self.params.get('SOURCE_MODEL_LT_RANDOM_SEED', None))

        gmpe_generator = random.Random()
        gmpe_generator.seed(self.params.get('GMPE_LT_RANDOM_SEED', None))

        gmf_generator = random.Random()
        gmf_generator.seed(self.params.get('GMF_RANDOM_SEED', None))

        histories = int(self.params['NUMBER_OF_SEISMICITY_HISTORIES'])
        realizations = int(self.params['NUMBER_OF_LOGIC_TREE_SAMPLES'])
        LOG.info(
            "Going to run hazard for %s histories of %s realizations each."
            % (histories, realizations))

        for i in range(0, histories):
            pending_tasks = []
            for j in range(0, realizations):
                self.store_source_model(source_model_generator.getrandbits(32))
                self.store_gmpe_map(gmpe_generator.getrandbits(32))
                stochastic_set_id = "%s!%s" % (i, j)
                pending_tasks.append(
                    tasks.compute_ground_motion_fields.delay(
                        self.id, self.sites_for_region(), stochastic_set_id,
                        gmf_generator.getrandbits(32)))

            for task in pending_tasks:
                task.wait()
                if task.status != 'SUCCESS':
                    raise Exception(task.result)

            for j in range(0, realizations):
                stochastic_set_id = "%s!%s" % (i, j)
                stochastic_set_key = kvs.generate_product_key(
                    self.id, kvs.tokens.STOCHASTIC_SET_TOKEN,
                    stochastic_set_id)
                print "Writing output for ses %s" % stochastic_set_key
                ses = kvs.get_value_json_decoded(stochastic_set_key)
                if ses:
                    results.extend(self.write_gmf_files(ses))
        return results

    def write_gmf_files(self, ses):
        """Generate a GeoTiff file and a NRML file for each GMF."""
        image_grid = self.region.grid
        iml_list = [float(param)
                    for param
                    in self.params['INTENSITY_MEASURE_LEVELS'].split(",")]

        LOG.debug("Generating GMF image, grid is %s col by %s rows" % (
                image_grid.columns, image_grid.rows))
        LOG.debug("IML: %s" % (iml_list))
        files = []
        for event_set in ses:
            for rupture in ses[event_set]:

                # NOTE(fab): we have to explicitly convert the JSON-decoded
                # tokens from Unicode to string, otherwise the path will not
                # be accepted by the GeoTiffFile constructor
                common_path = os.path.join(self.base_path, self['OUTPUT_DIR'],
                        "gmf-%s-%s" % (str(event_set.replace("!", "_")),
                                       str(rupture.replace("!", "_"))))
                tiff_path = "%s.tiff" % common_path
                nrml_path = "%s.xml" % common_path
                gwriter = geotiff.GMFGeoTiffFile(tiff_path, image_grid,
                    init_value=0.0, iml_list=iml_list,
                    discrete=True)
                xmlwriter = hazard_output.GMFXMLWriter(nrml_path)
                gmf_data = {}
                for site_key in ses[event_set][rupture]:
                    site = ses[event_set][rupture][site_key]
                    site_obj = shapes.Site(site['lon'], site['lat'])
                    point = image_grid.point_at(site_obj)
                    gwriter.write((point.row, point.column),
                        math.exp(float(site['mag'])))
                    gmf_data[site_obj] = \
                        {'groundMotion': math.exp(float(site['mag']))}

                gwriter.close()
                xmlwriter.serialize(gmf_data)
                files.append(tiff_path)
                files.append(gwriter.html_path)
                files.append(nrml_path)
        return files

    @preload
    def compute_ground_motion_fields(self, site_list, stochastic_set_id, seed):
        """Ground motion field calculation, runs on the workers."""
        jpype = java.jvm()

        jsite_list = self.parameterize_sites(site_list)
        key = kvs.generate_product_key(
            self.id, kvs.tokens.STOCHASTIC_SET_TOKEN, stochastic_set_id)
        gmc = self.params['GROUND_MOTION_CORRELATION']
        correlate = (gmc == "true" and True or False)
        java.jclass("HazardCalculator").generateAndSaveGMFs(
                self.cache, key, stochastic_set_id, jsite_list,
                 self.generate_erf(),
                self.generate_gmpe_map(),
                java.jclass("Random")(seed),
                jpype.JBoolean(correlate))


def gmf_id(history_idx, realization_idx, rupture_idx):
    """ Return a GMF id suitable for use as a KVS key """
    return "%s!%s!%s" % (history_idx, realization_idx, rupture_idx)


def _is_realization_hazard_curve_key(kvs_key):
    return (tokens.extract_product_type_from_kvs_key(kvs_key) == \
                tokens.HAZARD_CURVE_KEY_TOKEN)


def _is_mean_hazard_curve_key(kvs_key):
    return (tokens.extract_product_type_from_kvs_key(kvs_key) == \
                tokens.MEAN_HAZARD_CURVE_KEY_TOKEN)


def _is_quantile_hazard_curve_key(kvs_key):
    return (tokens.extract_product_type_from_kvs_key(kvs_key) == \
                tokens.QUANTILE_HAZARD_CURVE_KEY_TOKEN)


def _is_mean_hazmap_key(kvs_key):
    return (tokens.extract_product_type_from_kvs_key(kvs_key) == \
                tokens.MEAN_HAZARD_MAP_KEY_TOKEN)


def _is_quantile_hazmap_key(kvs_key):
    return (tokens.extract_product_type_from_kvs_key(kvs_key) == \
                tokens.QUANTILE_HAZARD_MAP_KEY_TOKEN)


def hazard_curve_filename(filename_part):
    return "%s-%s.xml" % (HAZARD_CURVE_FILENAME_PREFIX, filename_part)


def hazard_map_filename(filename_part):
    return "%s-%s.xml" % (HAZARD_MAP_FILENAME_PREFIX, filename_part)


def realization_hc_filename(realization):
    return hazard_curve_filename(realization)


def mean_hc_filename():
    return hazard_curve_filename('mean')


def quantile_hc_filename(quantile_value):
    filename_part = "quantile-%.2f" % quantile_value
    return hazard_curve_filename(filename_part)


def mean_hm_filename(poe):
    filename_part = "%s-mean" % poe
    return hazard_map_filename(filename_part)


def quantile_hm_filename(quantile_value, poe):
    filename_part = "%s-quantile-%.2f" % (poe, quantile_value)
    return hazard_map_filename(filename_part)


def _collect_curve_keys_per_quantile(keys):
    quantile_values = {}
    while len(keys) > 0:
        quantile_key = keys.pop()
        curr_qv = tokens.quantile_value_from_hazard_curve_key(
            quantile_key)
        if curr_qv not in quantile_values:
            quantile_values[curr_qv] = []
        quantile_values[curr_qv].append(quantile_key)
    return quantile_values


def _collect_map_keys_per_quantile(keys):
    quantile_values = {}
    while len(keys) > 0:
        quantile_key = keys.pop()
        curr_qv = tokens.quantile_value_from_hazard_map_key(
            quantile_key)
        if curr_qv not in quantile_values:
            quantile_values[curr_qv] = []
        quantile_values[curr_qv].append(quantile_key)
    return quantile_values


def create_hazardmap_writer(params, nrml_path):
    """Create a hazard map writer observing the settings in the config file.

    :param dict params: the settings from the OpenQuake engine configuration
        file.
    :param str nrml_path: the full path of the XML/NRML representation of the
        hazard map.
    :returns: an :py:class:`output.hazard.HazardMapXMLWriter` or an
        :py:class:`output.hazard.HazardMapDBWriter` instance.
    """
    db_flag = params.get("SERIALIZE_MAPS_TO_DB")
    if not db_flag or db_flag.lower() == "false":
        return hazard_output.HazardMapXMLWriter(nrml_path)
    else:
        job_db_key = params.get("OPENQUAKE_JOB_ID")
        assert job_db_key, "No job db key in the configuration parameters"
        job_db_key = int(job_db_key)
        session = get_uiapi_writer_session()
        return hazard_output.HazardMapDBWriter(session, nrml_path, job_db_key)


job.HazJobMixin.register("Event Based", EventBasedMixin, order=0)
job.HazJobMixin.register("Classical", ClassicalMixin, order=1)
