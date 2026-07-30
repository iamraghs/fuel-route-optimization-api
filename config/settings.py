"""
Django settings for Spotter AI Fuel Routing API.
Production-grade configuration for optimal performance.
"""

from pathlib import Path
from decouple import config

# Build paths inside the project
BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent

# Security Settings
SECRET_KEY = config('SECRET_KEY', default='django-insecure-dev-key-change-in-production')
DEBUG = config('DEBUG', default=True, cast=bool)
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1,testserver').split(',')

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.auth',
    'django.contrib.gis',
    'rest_framework',
    'corsheaders',
    'drf_spectacular',
    'fuel_routing',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# Database Configuration - PostgreSQL with PostGIS for BOTH development and production
DATABASES = {
    'default': {
        'ENGINE': config('DB_ENGINE', default='django.contrib.gis.db.backends.postgis'),
        'NAME': config('DB_NAME', default='fuel_routing_dev'),
        'USER': config('DB_USER', default='postgres'),
        'PASSWORD': config('DB_PASSWORD', default='postgres'),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
        'CONN_MAX_AGE': 600,
        'OPTIONS': {
            'options': '-c statement_timeout=30000'  # 30s query timeout — prevents hanging connections
        },
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# Media files
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Django REST Framework Configuration
REST_FRAMEWORK = {
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 100,
    'DEFAULT_FILTER_BACKENDS': [
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '1000/hour',
    },
}

# CORS Configuration
CORS_ALLOW_ALL_ORIGINS = True

# CORS Configuration
CORS_ALLOWED_ORIGINS = config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:3000,http://localhost:8000,http://127.0.0.1:8000/'
).split(',')

# Logging Configuration
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'filters': {
        'require_debug_false': {
            '()': 'django.utils.log.RequireDebugFalse',
        },
    },
    'handlers': {
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'simple'
        },
        'file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs' / 'fuel_routing.log',
            'maxBytes': 1024 * 1024 * 15,  # 15MB
            'backupCount': 10,
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'fuel_routing': {
            'handlers': ['console', 'file'],
            'level': 'DEBUG',
            'propagate': False,
        },
    },
}

# ============================================================================
# GOOGLE APIs Configuration
# ============================================================================
GOOGLE_MAPS_API_KEY = config('GOOGLE_MAPS_API_KEY', default='AIzaSyBSyzO1BUpxasIHBSz89hz7QeQzRLXUyuA')
GOOGLE_ROUTES_API_ENDPOINT = 'https://routes.googleapis.com/directions/v2:computeRoutes'
GOOGLE_GEOCODING_API_ENDPOINT = 'https://maps.googleapis.com/maps/api/geocode/json'
GOOGLE_API_TIMEOUT_SECONDS = 10
GOOGLE_ROUTES_API_REQUEST_TIMEOUT = 15

# ============================================================================
# VEHICLE SPECIFICATIONS
# ============================================================================
VEHICLE_FUEL_EFFICIENCY = 10  # Miles per gallon (MPG)
VEHICLE_FUEL_TANK_CAPACITY = 50  # Gallons (full tank)
VEHICLE_MAX_RANGE = 500  # Miles (tank capacity × MPG)
VEHICLE_RESERVE_RANGE_MILES = 50  # Reserve: 50 miles minimum

# ============================================================================
# FUEL STOP OPTIMIZATION PARAMETERS
# ============================================================================
FUEL_STOP_CORRIDOR_BUFFER_MILES = 50
FUEL_STOP_MAX_DETOUR_MILES = 5

# ============================================================================
# CACHING CONFIGURATION (Redis-first architecture)
# ============================================================================
# Production: Redis for sub-100ms response times
# Development: Local memory cache for testing

REDIS_ENABLED = config('REDIS_ENABLED', default='False') == 'True'

if REDIS_ENABLED:
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': config('REDIS_URL', default='redis://127.0.0.1:6379/1'),
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
                'CONNECTION_POOL_KWARGS': {'max_connections': 50, 'retry_on_timeout': True},
                'SOCKET_CONNECT_TIMEOUT': 5,
                'SOCKET_TIMEOUT': 5,
                'COMPRESSOR': 'django_redis.compressors.zlib.ZlibCompressor',
            },
            'KEY_PREFIX': 'fuel_routing',
            'TIMEOUT': 3600,
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'fuel-routing-cache',
            'TIMEOUT': 3600,
        }
    }

# Cache key prefixes and TTL
CACHE_KEYS = {
    'ROUTE_CACHE_PREFIX': 'route:',  # route:{md5_hash}
    'OPTIMIZATION_CACHE_PREFIX': 'optimization:',  # optimization:{route_hash}:{version}
    'PRICE_VERSION_KEY': 'price:version',
    'GEOCODE_CACHE_PREFIX': 'geocode:',  # geocode:{address_hash}
}

CACHE_TTL = {
    'ROUTE_GEOMETRY': 86400,  # 24 hours (route geometry is stable)
    'OPTIMIZATION': 3600,  # 1 hour (prices change)
    'GEOCODING': 604800,  # 7 days (geocoding is stable)
    'PRICE_VERSION': None,  # Never expire (explicit invalidation)
}

# Session configuration
CACHE_MIDDLEWARE_SECONDS = 3600
CACHE_MIDDLEWARE_KEY_PREFIX = 'fuel_routing'
SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
SESSION_CACHE_ALIAS = 'default'



