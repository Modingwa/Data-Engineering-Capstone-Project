import pandas as pd
import os
import configparser
import datetime as dt

from pyspark.sql import SparkSession
from pyspark.sql.functions import avg
from pyspark.sql import SQLContext
from pyspark.sql.functions import isnan, when, count, col, udf, dayofmonth, dayofweek, month, year, weekofyear
from pyspark.sql.functions import monotonically_increasing_id
from pyspark.sql.types import *

from utility import aggregate_temperature_data


def create_immigration_fact_table(spark, df, output_data):
    """This function creates an country dimension from the immigration and global land temperatures data.

    :param spark: spark session
    :param df: spark dataframe of immigration events
    :param visa_type_df: spark dataframe of global land temperatures data.
    :param output_data: path to write dimension dataframe to
    :return: spark dataframe representing calendar dimension
    """
    # get visa_type dimension
    dim_df = get_visa_type_dimension(spark, output_data)

    # create a view for visa type dimension
    dim_df.createOrReplaceTempView("visa_view")

    # create a udf to convert arrival date in SAS format to datetime object
    get_datetime = udf(lambda x: (dt.datetime(1960, 1, 1).date() + dt.timedelta(x)).isoformat() if x else None)

    # rename columns to align with data model
    df = df.withColumnRenamed('ccid', 'record_id') \
        .withColumnRenamed('i94res', 'country_residence_code') \
        .withColumnRenamed('i94addr', 'state_code')

    # create an immigration view
    df.createOrReplaceTempView("immigration_view")

    # create visa_type key
    df = spark.sql(
        """
        SELECT 
            immigration_view.*, 
            visa_view.visa_type_key
        FROM immigration_view
        LEFT JOIN visa_view ON visa_view.visatype=immigration_view.visatype
        """
    )

    # convert arrival date into datetime object
    df = df.withColumn("arrdate", get_datetime(df.arrdate))

    # drop visatype key
    df = df.drop(df.visatype)

    # write dimension to parquet file
    df.write.parquet(output_data + "immigration_fact", mode="overwrite")

    return df


def create_demographics_dimension_table(df, output_data):
    """This function creates a us demographics dimension table from the us cities demographics data.

    :param df: spark dataframe of us demographics survey data
    :param output_data: path to write dimension dataframe to
    :return: spark dataframe representing demographics dimension
    """
    dim_df = df.withColumnRenamed('Median Age', 'median_age') \
        .withColumnRenamed('Male Population', 'male_population') \
        .withColumnRenamed('Female Population', 'female_population') \
        .withColumnRenamed('Total Population', 'total_population') \
        .withColumnRenamed('Number of Veterans', 'number_of_veterans') \
        .withColumnRenamed('Foreign-born', 'foreign_born') \
        .withColumnRenamed('Average Household Size', 'average_household_size') \
        .withColumnRenamed('State Code', 'state_code')
    # lets add an id column
    dim_df = dim_df.withColumn('id', monotonically_increasing_id())

    # write dimension to parquet file
    dim_df.write.parquet(output_data + "demographics", mode="overwrite")

    return dim_df


def create_visa_type_dimension_table(df, output_data):
    """This function creates a visa type dimension from the immigration data.

    :param df: spark dataframe of immigration events
    :param output_data: path to write dimension dataframe to
    :return: spark dataframe representing calendar dimension
    """
    # create visatype df from visatype column
    visatype_df = df.select(['visatype']).distinct()

    # add an id column
    visatype_df = visatype_df.withColumn('visa_type_key', monotonically_increasing_id())

    # write dimension to parquet file
    visatype_df.write.parquet(output_data + "visatype", mode="overwrite")

    return visatype_df


def get_visa_type_dimension(spark, output_data):
    return spark.read.parquet(output_data + "visatype")


def create_country_dimension_table(spark, df, temp_df, output_data, mapping_file):
    """This function creates a country dimension from the immigration and global land temperatures data.

    :param spark: spark session object
    :param df: spark dataframe of immigration events
    :temp_df: spark dataframe of global land temperatures data.
    :param output_data: path to write dimension dataframe to
    :param mapping_file: csv file that maps country codes to country names
    :return: spark dataframe representing calendar dimension
    """
    # create temporary view for immigration data
    df.createOrReplaceTempView("immigration_view")

    # create temporary view for countries codes data
    mapping_file.createOrReplaceTempView("country_codes_view")

    # get the aggregated temperature data
    agg_temp = aggregate_temperature_data(temp_df)
    # create temporary view for countries average temps data
    agg_temp.createOrReplaceTempView("average_temperature_view")

    # create country dimension using SQL
    country_df = spark.sql(
        """
        SELECT 
            i94res as country_code,
            Name as country_name
        FROM immigration_view
        LEFT JOIN country_codes_view
        ON immigration_view.i94res=country_codes_view.code
        """
    ).distinct()
    # create temp country view
    country_df.createOrReplaceTempView("country_view")

    country_df = spark.sql(
        """
        SELECT 
            country_code,
            country_name,
            average_temperature
        FROM country_view
        LEFT JOIN average_temperature_view
        ON country_view.country_name=average_temperature_view.Country
        """
    ).distinct()

    # write the dimension to a parquet file
    country_df.write.parquet(output_data + "country", mode="overwrite")

    return country_df


def create_immigration_calendar_dimension(df, output_data):
    """This function creates an immigration calendar based on arrival date

    :param df: spark dataframe of immigration events
    :param output_data: path to write dimension dataframe to
    :return: spark dataframe representing calendar dimension
    """
    # create a udf to convert arrival date in SAS format to datetime object
    get_datetime = udf(lambda x: (dt.datetime(1960, 1, 1).date() + dt.timedelta(x)).isoformat() if x else None)

    # create initial calendar df from arrdate column
    calendar_df = df.select(['arrdate']).withColumn("arrdate", get_datetime(df.arrdate)).distinct()

    # expand df by adding other calendar columns
    calendar_df = calendar_df.withColumn('arrival_day', dayofmonth('arrdate'))
    calendar_df = calendar_df.withColumn('arrival_week', weekofyear('arrdate'))
    calendar_df = calendar_df.withColumn('arrival_month', month('arrdate'))
    calendar_df = calendar_df.withColumn('arrival_year', year('arrdate'))
    calendar_df = calendar_df.withColumn('arrival_weekday', dayofweek('arrdate'))

    # create an id field in calendar df
    calendar_df = calendar_df.withColumn('id', monotonically_increasing_id())

    # write the calendar dimension to parquet file
    partition_columns = ['arrival_year', 'arrival_month', 'arrival_week']
    calendar_df.write.parquet(output_data + "immigration_calendar", partitionBy=partition_columns, mode="overwrite")

    return calendar_df


# Perform quality checks here
def quality_checks(df, table_name):
    """Count checks on fact and dimension table to ensure completeness of data.

    :param df: spark dataframe to check counts on
    :param table_name: corresponding name of table
    """
    total_count = df.count()

    if total_count == 0:
        print(f"Data quality check failed for {table_name} with zero records!")
    else:
        print(f"Data quality check passed for {table_name} with {total_count:,} records.")
    return 0
