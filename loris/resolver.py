# -*- coding: utf-8 -*-
"""
`resolver` -- Resolve Identifiers to Image Paths
================================================
"""
from logging import getLogger
from loris_exception import ResolverException
from os.path import join, exists, isfile
from os import makedirs
from os.path import dirname
from shutil import copy
from urllib import unquote, quote_plus
from contextlib import closing

import constants
import hashlib
import glob
import requests
import re

from PIL import Image

logger = getLogger(__name__)

class _AbstractResolver(object):
    def __init__(self, config):
        self.config = config

    def is_resolvable(self, ident):
        """
        The idea here is that in some scenarios it may be cheaper to check 
        that an id is resolvable than to actually resolve it. For example, for 
        an HTTP resolver, this could be a HEAD instead of a GET.

        Args:
            ident (str):
                The identifier for the image.
        Returns:
            bool
        """
        cn = self.__class__.__name__
        raise NotImplementedError('is_resolvable() not implemented for %s' % (cn,))

    def resolve(self, ident):
        """
        Given the identifier of an image, get the path (fp) and format (one of. 
        'jpg', 'tif', or 'jp2'). This will likely need to be reimplemented for
        environments and can be as smart or dumb as you want.
        
        Args:
            ident (str):
                The identifier for the image.
        Returns:
            (str, str): (fp, format)
        Raises:
            ResolverException when something goes wrong...
        """
        cn = self.__class__.__name__
        raise NotImplementedError('resolve() not implemented for %s' % (cn,))


class SimpleFSResolver(_AbstractResolver):

    def __init__(self, config):
        super(SimpleFSResolver, self).__init__(config)
        self.cache_root = self.config['src_img_root']

    def is_resolvable(self, ident):
        ident = unquote(ident)
        fp = join(self.cache_root, ident)
        return exists(fp)

    @staticmethod
    def _format_from_ident(ident):
        return ident.split('.')[-1]

    def resolve(self, ident):
        # For this dumb version a constant path is prepended to the identfier 
        # supplied to get the path It assumes this 'identifier' ends with a file 
        # extension from which the format is then derived.
        ident = unquote(ident)
        fp = join(self.cache_root, ident)
        logger.debug('src image: %s' % (fp,))

        if not exists(fp):
            public_message = 'Source image not found for identifier: %s.' % (ident,)
            log_message = 'Source image not found at %s for identifier: %s.' % (fp,ident)
            logger.warn(log_message)
            raise ResolverException(404, public_message)

        format = SimpleFSResolver._format_from_ident(ident)
        logger.debug('src format %s' % (format,))

        return (fp, format)

#To use this the resolver stanza of the config will have to have both the
#src_img_root as required by the SimpleFSResolver and also an
#[[extension_map]], which will be a hash mapping found extensions to the
#extensions that loris wants to see, e.g.
# [resolver]
# impl = 'loris.resolver.ExtensionNormalizingFSResolver'
# src_img_root = '/cnfs-ro/iiif/production/medusa-root' # r--
#   [[extension_map]]
#   jpeg = 'jpg'
#   tiff = 'tif'
#Note that case normalization happens before looking up in the extension_map.
class ExtensionNormalizingFSResolver(SimpleFSResolver):
    def __init__(self, config):
        super(ExtensionNormalizingFSResolver, self).__init__(config)
        self.extension_map = self.config['extension_map']

    def resolve(self, ident):
        fp, format = super(ExtensionNormalizingFSResolver, self).resolve(ident)
        format = format.lower()
        format = self.extension_map.get(format, format)
        return (fp, format)

class SimpleHTTPResolver(_AbstractResolver):
    '''
    Example resolver that one might use if image files were coming from
    an http image store (like Fedora Commons). The first call to `resolve()`
    copies the source image into a local cache; subsequent calls use local
    copy from the cache.

    The config dictionary MUST contain
     * `cache_root`, which is the absolute path to the directory where source images
        should be cached.

    The config dictionary MAY contain
     * `source_prefix`, the url up to the identifier.
     * `source_suffix`, the url after the identifier (if applicable).
     * `default_format`, the format of images (will use content-type of response if not specified).
     * `head_resolvable` with value True, whether to make HEAD requests to verify object existence (don't set if using
        Fedora Commons prior to 3.8).
     * `uri_resolvable` with value True, allows one to use full uri's to resolve to an image.
     * `user`, the username to make the HTTP request as.
     * `pw`, the password to make the HTTP request as.
    '''
    def __init__(self, config):
        super(SimpleHTTPResolver, self).__init__(config)

        self.source_prefix = self.config.get('source_prefix', '')

        self.source_suffix = self.config.get('source_suffix', '')

        self.default_format = self.config.get('default_format', None)

        self.head_resolvable = self.config.get('head_resolvable', False)

        self.uri_resolvable = self.config.get('uri_resolvable', False)

        self.user = self.config.get('user', None)

        self.pw = self.config.get('pw', None)

        self.ssl_check = self.config.get('ssl_check', True)

        if 'cache_root' in self.config:
            self.cache_root = self.config['cache_root']
        else:
            message = 'Server Side Error: Configuration incomplete and cannot resolve. Missing setting for cache_root.'
            logger.error(message)
            raise ResolverException(500, message)

        if not self.uri_resolvable and self.source_prefix == '':
            message = 'Server Side Error: Configuration incomplete and cannot resolve. Must either set uri_resolvable' \
                      ' or source_prefix settings.'
            logger.error(message)
            raise ResolverException(500, message)

    def request_options(self):
        # parameters to pass to all head and get requests;
        # currently only authorization, if configured
        if self.user is not None and self.pw is not None:
            return {'auth': (self.user, self.pw)}
        return {}

    def is_resolvable(self, ident):
        ident = unquote(ident)
        fp = join(self.cache_root, SimpleHTTPResolver._cache_subroot(ident))
        if exists(fp):
            return True
        else:
            fp = self._web_request_url(ident)

            if self.head_resolvable:
                try:
                    with closing(requests.head(fp, verify=self.ssl_check, **self.request_options())) as response:
                        if response.status_code is 200:
                            return True
                except requests.exceptions.MissingSchema:
                    return False

            else:
                try:
                    with closing(requests.get(fp, stream=True, verify=self.ssl_check, **self.request_options())) as response:
                        if response.status_code is 200:
                            return True
                except requests.exceptions.MissingSchema:
                    return False

        return False

    def format_from_ident(self, ident, potential_format):
        if self.default_format is not None:
            return self.default_format
        elif potential_format is not None:
            return potential_format
        elif ident.rfind('.') != -1 and (len(ident) - ident.rfind('.') <= 5):
            return ident.split('.')[-1]
        else:
            public_message = 'Format could not be determined for: %s.' % (ident,)
            log_message = 'Format could not be determined for: %s.' % (ident)
            logger.warn(log_message)
            raise ResolverException(404, public_message)

    def _web_request_url(self, ident):
        if (ident[0:6] == 'http:/' or ident[0:7] == 'https:/') and self.uri_resolvable:
            # ident is http request with no prefix or suffix specified
            # For some reason, identifier is http:/<url> or https:/<url>? 
            # Hack to correct without breaking valid urls.
            first_slash = ident.find('/')
            return '%s//%s' % (ident[:first_slash], ident[first_slash:].lstrip('/'))
        else:
            return self.source_prefix + ident + self.source_suffix

    #Get a subdirectory structure for the cache_subroot through hashing.
    @staticmethod
    def _cache_subroot(ident):
        cache_subroot = ''

        #Split out potential pidspaces... Fedora Commons most likely use case.
        if ident[0:6] != 'http:/' and ident[0:7] != 'https:/' and len(ident.split(':')) > 1:
            for split_ident in ident.split(':')[0:-1]:
                cache_subroot = join(cache_subroot, split_ident)
        elif ident[0:6] == 'http:/' or ident[0:7] == 'https:/':
            cache_subroot = 'http'

        cache_subroot = join(cache_subroot, SimpleHTTPResolver._ident_file_structure(ident))

        return cache_subroot

    #Get the directory structure of the identifier itself
    @staticmethod
    def _ident_file_structure(ident):
        file_structure = ''
        ident_hash = hashlib.md5(quote_plus(ident)).hexdigest()
        #First level 2 digit directory then do three digits...
        file_structure_list = [ident_hash[0:2]] + [ident_hash[i:i+3] for i in range(2, len(ident_hash), 3)]

        for piece in file_structure_list:
            file_structure = join(file_structure, piece)

        return file_structure

    def resolve(self, ident):
        ident = unquote(ident)

        local_fp = join(self.cache_root, SimpleHTTPResolver._cache_subroot(ident))
        local_fp = join(local_fp)

        if exists(local_fp):
            cached_object = glob.glob(join(local_fp, 'loris_cache.*'))

            if len(cached_object) > 0:
                cached_object = cached_object[0]
            else:
                public_message = 'Cached image not found for identifier: %s.' % (ident)
                log_message = 'Cached image not found for identifier: %s. Empty directory where image expected?' % (ident)
                logger.warn(log_message)
                raise ResolverException(404, public_message)

            format = self.format_from_ident(cached_object,None)
            logger.debug('src image from local disk: %s' % (cached_object,))
            return (cached_object, format)
        else:
            fp = self._web_request_url(ident)

            logger.debug('src image: %s' % (fp,))

            try:
                response = requests.get(fp, stream = False, verify=self.ssl_check, **self.request_options())
            except requests.exceptions.MissingSchema:
                public_message = 'Bad URL request made for identifier: %s.' % (ident,)
                log_message = 'Bad URL request at %s for identifier: %s.' % (fp,ident)
                logger.warn(log_message)
                raise ResolverException(404, public_message)

            if response.status_code != 200:
                public_message = 'Source image not found for identifier: %s. Status code returned: %s' % (ident,response.status_code)
                log_message = 'Source image not found at %s for identifier: %s. Status code returned: %s' % (fp,ident,response.status_code)
                logger.warn(log_message)
                raise ResolverException(404, public_message)

            if 'content-type' in response.headers:
                try:
                    format = self.format_from_ident(ident, constants.FORMATS_BY_MEDIA_TYPE[response.headers['content-type']])
                except KeyError:
                    logger.warn('Your server may be responding with incorrect content-types. Reported %s for ident %s.'
                                % (response.headers['content-type'],ident))
                    #Attempt without the content-type
                    format = self.format_from_ident(ident, None)
            else:
                format = self.format_from_ident(ident, None)

            logger.debug('src format %s' % (format,))

            local_fp = join(local_fp, "loris_cache." + format)

            try:
                makedirs(dirname(local_fp))
            except:
                logger.debug("Directory already existed... possible problem if not a different format")

            with open(local_fp, 'wb') as fd:
                for chunk in response.iter_content(2048):
                    fd.write(chunk)

            logger.info("Copied %s to %s" % (fp, local_fp))

            return (local_fp, format)


class TemplateHTTPResolver(SimpleHTTPResolver):
    '''HTTP resolver that suppors multiple configurable patterns for supported
    urls.  Based on SimpleHTTPResolver.  Identifiers in URLs should be
    specified as `template_name:id`.

    The configuration MUST contain
     * `cache_root`, which is the absolute path to the directory where source images
        should be cached.

    The configuration SHOULD contain
     * `templates`, a comma-separated list of template names e.g.
        templates=`site1,site2`
     * A url pattern for each specified template, e.g.
       site1='http://example.edu/images/%s' or site2='http://example.edu/images/%s/master'

    Note that if a template is listed but has no pattern configured, the
    resolver will warn but not fail.

    The configuration may also include the following settings, as used by
    SimpleHTTPResolver:
     * `default_format`, the format of images (will use content-type of
        response if not specified).
     * `head_resolvable` with value True, whether to make HEAD requests
        to verify object existence (don't set if using Fedora Commons
        prior to 3.8).  [Currently must be the same for all templates]
    '''
    def __init__(self, config):
        super(SimpleHTTPResolver, self).__init__(config)
        templates = self.config.get('templates', '')
        # technically it's not an error to have no templates configured,
        # but nothing will resolve; is that useful? or should this
        # cause an exception?
        if not templates:
            logger.warn('No templates specified in configuration')
        self.templates = {}
        for name in templates.split(','):
            name = name.strip()
            cfg = self.config.get(name, None)
            if cfg is None:
                logger.warn('No configuration specified for resolver template %s' % name)
            else:
                self.templates[name] = cfg

        # inherited/required configs from simple http resolver

        self.head_resolvable = self.config.get('head_resolvable', False)
        self.default_format = self.config.get('default_format', None)
        if 'cache_root' in self.config:
            self.cache_root = self.config['cache_root']
        else:
            message = 'Server Side Error: Configuration incomplete and cannot resolve. Missing setting for cache_root.'
            logger.error(message)
            raise ResolverException(500, message)

        # required for simplehttpresolver
        # all templates are assumed to be uri resolvable
        self.uri_resolvable = True
                
        self.ssl_check = self.config.get('ssl_check', True)

    def _web_request_url(self, ident):
        # only split identifiers that look like template ids; ignore other requests (e.g. favicon)
        if ':' not in ident:
            return
        prefix, ident = ident.split(':', 1)

        if 'delimiter' in self.config:
            # uses delimiter of choice from config file to split identifier
            # into tuple that will be fed to template
            ident_components = ident.split(self.config['delimiter'])
            if prefix in self.templates:
                return self.templates[prefix] % tuple(ident_components)
        else:
            if prefix in self.templates:
                return self.templates[prefix] % ident
        # if prefix is not recognized, no identifier is returned
        # and loris will return a 404

    def request_options(self):
        # currently no username/passsword supported
        return {}


class SourceImageCachingResolver(_AbstractResolver):
    '''
    Example resolver that one might use if image files were coming from
    mounted network storage. The first call to `resolve()` copies the source
    image into a local cache; subsequent calls use local copy from the cache.
 
    The config dictionary MUST contain 
     * `cache_root`, which is the absolute path to the directory where images 
        should be cached.
     * `source_root`, the root directory for source images.
    '''
    def __init__(self, config):
        super(SourceImageCachingResolver, self).__init__(config)
        self.cache_root = self.config['cache_root']
        self.source_root = self.config['source_root']

    def is_resolvable(self, ident):
        ident = unquote(ident)
        fp = join(self.source_root, ident)
        return exists(fp)

    @staticmethod
    def _format_from_ident(ident):
        return ident.split('.')[-1]

    def resolve(self, ident):
        ident = unquote(ident)
        local_fp = join(self.cache_root, ident)

        if exists(local_fp):
            format = SourceImageCachingResolver._format_from_ident(ident)
            logger.debug('src image from local disk: %s' % (local_fp,))
            return (local_fp, format)
        else:
            fp = join(self.source_root, ident)
            logger.debug('src image: %s' % (fp,))
            if not exists(fp):
                public_message = 'Source image not found for identifier: %s.' % (ident,)
                log_message = 'Source image not found at %s for identifier: %s.' % (fp,ident)
                logger.warn(log_message)
                raise ResolverException(404, public_message)

            makedirs(dirname(local_fp))
            copy(fp, local_fp)
            logger.info("Copied %s to %s" % (fp, local_fp))

            format = SourceImageCachingResolver._format_from_ident(ident)
            logger.debug('src format %s' % (format,))

            return (local_fp, format)



class OsuSimpleHTTPResolver(_AbstractResolver):
    '''
    Example resolver that one might use if image files were coming from
    an http image store (like Fedora Commons). The first call to `resolve()`
    copies the source image into a local cache; subsequent calls use local
    copy from the cache.

    The config dictionary MUST contain
     * `cache_root`, which is the absolute path to the directory where source images
        should be cached.

    The config dictionary MAY contain
     * `source_prefix`, the url up to the identifier.
     * `source_suffix`, the url after the identifier (if applicable).
     * `default_format`, the format of images (will use content-type of response if not specified).
     * `head_resolvable` with value True, whether to make HEAD requests to verify object existence (don't set if using
        Fedora Commons prior to 3.8).
     * `uri_resolvable` with value True, allows one to use full uri's to resolve to an image.
     * `user`, the username to make the HTTP request as.
     * `pw`, the password to make the HTTP request as.
    '''
    def __init__(self, config):
        super(OsuSimpleHTTPResolver, self).__init__(config)

        self.source_prefix = self.config.get('source_prefix', '')
        self.source_suffix = self.config.get('source_suffix', '')
        self.default_format = self.config.get('default_format', None)
        self.head_resolvable = self.config.get('head_resolvable', False)
        self.uri_resolvable = self.config.get('uri_resolvable', False)
        self.user = self.config.get('user', None)
        self.pw = self.config.get('pw', None)
        self.ssl_check = self.config.get('ssl_check', True)

        self.lowres_suffix = '-lowres'

        if 'cache_root' in self.config:
            self.cache_root = self.config['cache_root']
        else:
            message = 'Server Side Error: Configuration incomplete and cannot resolve. Missing setting for cache_root.'
            logger.error(message)
            raise ResolverException(500, message)

        if not self.uri_resolvable and self.source_prefix == '':
            message = 'Server Side Error: Configuration incomplete and cannot resolve. Must either set uri_resolvable' \
                      ' or source_prefix settings.'
            logger.error(message)
            raise ResolverException(500, message)

    def request_options(self):
        # parameters to pass to all head and get requests;
        # currently only authorization, if configured
        if self.user is not None and self.pw is not None:
            return {'auth': (self.user, self.pw)}
        return {}

    def is_resolvable(self, ident):
        ident = unquote(ident)
        fp = join(self.cache_root, OsuSimpleHTTPResolver._cache_subroot(ident))
        if exists(fp):
            return True
        else:
            fp = self._web_request_url(ident)

            if self.head_resolvable:
                try:
                    with closing(requests.head(fp, verify=self.ssl_check, **self.request_options())) as response:
                        if response.status_code is 200:
                            return True
                except requests.exceptions.MissingSchema:
                    return False

            else:
                try:
                    with closing(requests.get(fp, stream=True, verify=self.ssl_check, **self.request_options())) as response:
                        if response.status_code is 200:
                            return True
                except requests.exceptions.MissingSchema:
                    return False

        return False

    def format_from_ident(self, ident, potential_format):
        if self.default_format is not None:
            return self.default_format
        elif potential_format is not None:
            return potential_format
        elif ident.rfind('.') != -1 and (len(ident) - ident.rfind('.') <= 5):
            return ident.split('.')[-1]
        else:
            public_message = 'Format could not be determined for: %s.' % (ident,)
            log_message = 'Format could not be determined for: %s.' % (ident)
            logger.warn(log_message)
            raise ResolverException(404, public_message)

    def _web_request_url(self, ident):
        if (ident[0:6] == 'http:/' or ident[0:7] == 'https:/') and self.uri_resolvable:
            # ident is http request with no prefix or suffix specified
            # For some reason, identifier is http:/<url> or https:/<url>? 
            # Hack to correct without breaking valid urls.
            first_slash = ident.find('/')
            return '%s//%s' % (ident[:first_slash], ident[first_slash:].lstrip('/'))
        else:
            #We'll strip out the derivative type and use it as suffix later
            if '-low_resolution' in ident:
                sourceSuffix = '/low_resolution'
                ident = ident.replace('-low_resolution', '')
            else:
                sourceSuffix = ''
                ident = ident.replace('-content', '')
            #else:
                #message = 'Could not find correct derivative for this request string: ' + ident
                #raise ResolverException(404, message)

            # we need to remove the version number and low res indicator from
            # the ident if it exists
            fedora_ident = self._strip_lowres(re.sub(r'-version(\d)+', '' , ident))
            return self.source_prefix + fedora_ident + sourceSuffix

    # Determine if ident is for a low resolution image
    def _is_lowres(self, ident):
        return self.lowres_suffix in ident

    # Remove the lowres indicator from an identifier
    def _strip_lowres(self, ident):
        return ident.replace(self.lowres_suffix, '')

    #Get a subdirectory structure for the cache_subroot through hashing.
    @staticmethod
    def _cache_subroot(ident):
        cache_subroot = ''

        #Split out potential pidspaces... Fedora Commons most likely use case.
        if ident[0:6] != 'http:/' and ident[0:7] != 'https:/' and len(ident.split(':')) > 1:
            for split_ident in ident.split(':')[0:-1]:
                cache_subroot = join(cache_subroot, split_ident)
        elif ident[0:6] == 'http:/' or ident[0:7] == 'https:/':
            cache_subroot = 'http'

        cache_subroot = join(cache_subroot, OsuSimpleHTTPResolver._ident_file_structure(ident))

        return cache_subroot

    #Get the directory structure of the identifier itself
    @staticmethod
    def _ident_file_structure(ident):
        file_structure = ''
        ident_hash = hashlib.md5(quote_plus(ident)).hexdigest()
        #First level 2 digit directory then do three digits...
        file_structure_list = [ident_hash[0:2]] + [ident_hash[i:i+3] for i in range(2, len(ident_hash), 3)]

        for piece in file_structure_list:
            file_structure = join(file_structure, piece)

        return file_structure

    # Create cache file path
    @staticmethod
    def _cache_file_path(fp, format):
        return join(fp, "loris_cache." + format)

    # Add the cache string to file path and make directory
    @staticmethod
    def _create_cache_directory(fp):
        try:
            makedirs(dirname(fp))
        except:
            logger.debug("Directory already existed... possible problem if not a different format")

    # Resize the lowres image based on DPI
    @staticmethod
    def _resize_lowres_image(image):
        logger.debug('Creating low resolution derivative.')
        logger.debug('Original image size is: %s', image.size)

        # If we have the image DPI information, cap at 150 DPI
        if 'dpi' in image.info and type(image.info['dpi']) is tuple:
            # Sometimes DPI info comes through as a float. Normalize to int.
            dpi = ( int(image.info['dpi'][0]), int(image.info['dpi'][1]) )
            logger.debug('Original image DPI is: %s', dpi)
            max_dpi = (150, 150)
            if dpi[0] > max_dpi[0] or dpi[1] > max_dpi[1]:
                logger.debug('Image resoluton higher than %s dpi, scaling down.', max_dpi)
                width = int(max_dpi[0] / float(dpi[0]) * image.size[0])
                height = int(max_dpi[1] / float(dpi[1]) * image.size[1])
                image = image.resize((width, height), Image.ANTIALIAS)

        # If the image has no DPI info, cap at 850x1100px (8.5"x11" @ 100dpi)
        else:
            logger.debug('No DPI information found in original image')
            dpi = None
            max_size = (850, 1100)
            if image.size[0] > max_size[0] or image.size[1] > max_size[1]:
                logger.debug('Image size larger than %s, scaling down.', max_size)
                image.thumbnail(max_size, Image.ANTIALIAS)

        logger.debug('Final low res image size is: %s', image.size)
        return image, dpi


    def _resolve_from_cache(self, ident, local_fp):
        logger.debug('Resolving image from cache...')
        cached_object = glob.glob(OsuSimpleHTTPResolver._cache_file_path(local_fp, '*'))

        if len(cached_object) > 0:
            cached_object = cached_object[0]
        else:
            public_message = 'Cached image not found for identifier: %s.' % (ident)
            log_message = 'Cached image not found for identifier: %s. Empty directory where image expected?' % (ident)
            logger.warn(log_message)
            raise ResolverException(404, public_message)

        format = self.format_from_ident(cached_object, None)
        logger.debug('src image from local disk: %s' % (cached_object,))
        return (cached_object, format)

    def _resolve_lowres(self, ident, local_fp):
        logger.debug('Resolving low resolution image...')

        # Fetch (and cache) the original image first
        base_ident = self._strip_lowres(ident)
        base_local_fp, base_format = self.resolve(base_ident)

        image, dpi = OsuSimpleHTTPResolver._resize_lowres_image(Image.open(base_local_fp))
        options = {'quality': 90}
        if dpi:
            options['dpi'] = dpi
        
        local_fp = OsuSimpleHTTPResolver._cache_file_path(local_fp, 'jpg')
        OsuSimpleHTTPResolver._create_cache_directory(local_fp)
        image.save(local_fp, 'JPEG', **options)

        logger.debug('Saved low resolution image to: %s', local_fp)
        return (local_fp, 'jpg')

    def _resolve_from_remote(self, ident, local_fp):
        logger.debug('Resolving remote image over HTTP...')
        fp = self._web_request_url(ident)
        logger.debug('src image: %s' % (fp,))

        try:
            response = requests.get(fp, stream = False, verify=self.ssl_check, **self.request_options())
        except requests.exceptions.MissingSchema:
            public_message = 'Bad URL request made for identifier: %s.' % (ident,)
            log_message = 'Bad URL request at %s for identifier: %s.' % (fp,ident)
            logger.warn(log_message)
            raise ResolverException(404, public_message)

        if response.status_code != 200:
            public_message = 'Source image not found for identifier: %s. Status code returned: %s' % (ident,response.status_code)
            log_message = 'Source image not found at %s for identifier: %s. Status code returned: %s' % (fp,ident,response.status_code)
            logger.warn(log_message)
            raise ResolverException(404, public_message)

        if 'content-type' in response.headers:
            try:
                format = self.format_from_ident(ident, constants.FORMATS_BY_MEDIA_TYPE[response.headers['content-type']])
            except KeyError:
                logger.warn('Your server may be responding with incorrect content-types. Reported %s for ident %s.'
                            % (response.headers['content-type'],ident))
                #Attempt without the content-type
                format = self.format_from_ident(ident, None)
        else:
            format = self.format_from_ident(ident, None)

        logger.debug('src format %s' % (format,))

        local_fp = OsuSimpleHTTPResolver._cache_file_path(local_fp, format)
        OsuSimpleHTTPResolver._create_cache_directory(local_fp)

        with open(local_fp, 'wb') as file:
            for chunk in response.iter_content(2048):
                file.write(chunk)

        logger.info("Copied %s to %s" % (fp, local_fp))
        return (local_fp, format)


    def resolve(self, ident):
        ident = unquote(ident)

        local_fp = join(self.cache_root, OsuSimpleHTTPResolver._cache_subroot(ident))
        local_fp = join(local_fp)

        if exists(local_fp):
            return self._resolve_from_cache(ident, local_fp)
        elif self._is_lowres(ident):
            return self._resolve_lowres(ident, local_fp)
        else:
            return self._resolve_from_remote(ident, local_fp)
