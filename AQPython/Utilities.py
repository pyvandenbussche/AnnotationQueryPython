import sys
import io
import os
from pyspark.sql.functions import *
from pyspark.sql.types import *
from pyspark.sql import SparkSession
from urllib.parse import quote_plus
from urllib.parse import unquote_plus

spark = SparkSession.builder.getOrCreate()

_ORIG = 'orig'
_ORIG_ANNOT_ID = 'origAnnotID'
_PARENT_ID = 'parentId'
_ATTR = 'attr'
_OM_NON_ATTRIBUTE_PROPERTIES = [_ORIG, _ORIG_ANNOT_ID, _PARENT_ID]
_OM_ANNOT_SET = 'om'
_WILDCARD = '*'

def GetAQAnnotations(df, props=[], lcProps=[], decodeProps=[], numPartitions=int(spark.conf.get('spark.sql.shuffle.partitions'))):
  """This function converts a Dataframe of CATAnnotations to a Dataframe of AQAnnotations.  
 
    A bare-bones AQAnnotation (with no properties) can be generated by only passing a Dataframe of CATAnnotations. 
    If properties (name-value pairs from the CATAnnotation other column) are desired, you have the option of specifying an Array of names (from these name-value pairs).  
    Additionally, you have the option of specifying if the values for these properties should be lower-cased and/or url decoded.

  Args:
    catAnnots: Dataframe of CATAnnotations
    props: Array of property names  (from the name-value pairs in the other column in CATAnnotation) that you would like populated in the AQAnnotation Map of properties.
    lcProps: Array of property names where the value should be lower cased when populating the AQAnnotation Map of properties.
    decodeProps: Array of property names where the value should be url decoded when populating the AQAnnotation Map of properties.
    numPartitions: Number of partitions for the Dataframe of AQAnnotations.

  Returns:
    Dataframe of AQAnnotations

  """
  
  def GetAQProperties(set, other, props=[], lcProps=[], decodeProps=[]):
    propsMap = {}
    attrBuf = []
    if len(props) > 0:
      otherToks = other.split('&')
      for otherTok in otherToks:
        toks = otherTok.split('=')
        if len(toks) == 2:
          key = toks[0]
          value = toks[1]
          if any(p in props for p in [key,_WILDCARD]):
            if any(p in decodeProps for p in [key,_WILDCARD]):
              value =  unquote_plus(value)
            if any(p in lcProps for p in [key,_WILDCARD]):
              value = value.lower()
            if _WILDCARD in props and set.lower() == _OM_ANNOT_SET and key not in _OM_NON_ATTRIBUTE_PROPERTIES:
              attrBuf.append(otherTok)
            else:
              propsMap[key] = value
          elif _ATTR in props and set.lower() == _OM_ANNOT_SET:
            if key not in _OM_NON_ATTRIBUTE_PROPERTIES:
              attrBuf.append(otherTok)
      if len(attrBuf) > 0 and _ATTR not in propsMap:
        propsMap[_ATTR] = '&'.join(map(str,attrBuf))
    return propsMap
        
  GetAQPropertiesUDF = udf(GetAQProperties,MapType(StringType(),StringType()))

  props_lit = array(*[lit(p) for p in props])
  lc_props_lit = array(*[lit(p) for p in lcProps])
  decode_props_lit = array(*[lit(p) for p in decodeProps])
  aqdf = df.withColumn('properties', GetAQPropertiesUDF(col('annotSet'),col('other'),props_lit,lc_props_lit,decode_props_lit)) \
           .drop('other','text') \
           .repartition(numPartitions,'docId') \
           .sortWithinPartitions('docId','startOffset','endOffset')
  return aqdf


def GetCATAnnotations(df, props=[], encodeProps=[]):
  """This function converts a Dataframe of AQAnnotations to a Dataframe of CATAnnotations.  

    If specific properties (name-value pairs to set in the CATAnnotation other column) are desired, you have the option of specifying an Array of names (for these name-value pairs). 
    Additionally, you have the option of specifying if the values for these name-value pairs that  should be url encoded.

  Args:
    aqAnnotations: Dataframe of AQAnnotations to convert to Dataframe of CATAnnotations
    props: Array of property names  to make name-value pairs in the other column of CATAnnotation.
    encodeProps: Array of property names  to url encode the value when making name-value pairs in the other column of CATAnnotation.
  
  Returns:
    Dataframe of CATAnnotations

  """

  def GetCATProperties(properties, props=[], encodeProps=[]):
    otherBuf = []
    if (properties != None):
      for prop in properties:
        if (_WILDCARD in props) or (prop in props):
          if (_WILDCARD in encodeProps) or (prop in encodeProps):
            otherBuf.append(prop + '=' + quote_plus(properties[prop]))
          else:
            otherBuf.append(prop + '=' + properties[prop])
      return '&'.join(map(str,otherBuf))
    else:
      return None

  GetCATPropertiesUDF = udf(GetCATProperties)

  props_lit = array(*[lit(p) for p in props])
  encode_props_lit = array(*[lit(p) for p in encodeProps])
  catdf = df.withColumn("other", GetCATPropertiesUDF(col("properties"),props_lit,encode_props_lit)) \
            .drop("properties") 
  return catdf


def Hydrate(df, txtPath, excludes=True):
  """This function will retrieve the text for each AQAnnotation in the passed Dataframe of AQAnnotations, populate the text property with this value in the AQAnnotation, and return a Dataframe of AQAnnotations with the text property populated.  
 
    Keep in mind that for 'text/word' annotations the orig column will already be populated with the 'original' text value so this may not be needed.  
    However, if you are working with sentence annotations (and other similar annotations) this could prove to be very helpful. 

  Args:
    df: The Dataframe of Annotations that we want to populate the text property with the text for this annotation
    textPath: Path the str files.  The str files for the documents in the ds annotations must be found here.
    excludes: Whether we want to include the 'excludes' text.  True means exclude the excluded text.

  Returns:
    Dataframe of AQAnnotations
  """

  def HydrateText(docId, startOffset, endOffset, properties, txtPath, excludes):
  
    # Read in the text for the document (want to think about ways for improving performance)
    docText = ''
    text = ''
  
    # Check if file already has been read (written to tmp space)
    if os.path.exists('/tmp/' + docId):
      with io.open('/tmp/' + docId,'r',encoding='utf-8') as f:
        docText = f.read()
    else:
      try:
        with io.open(txtPath + docId,'r',encoding='utf-8') as f:
          docText = f.read()
        with io.open('/tmp/' + docId,'w',encoding='utf-8') as f:
          f.write(docText)
      except Exception as ex:
        print(ex)
        docText=""
    
    # Return properties if docText was empty or 'text' is already defined in the properties
    if (docText == '') or ((properties != None) and ('text' in properties)):
      return properties
    else:
      if (excludes) and (properties != None) and ('excludes' in properties) and (len(properties['excludes']) > 0):
        excludes = []
        exToks = []
        for excludesEntry in properties['excludes'].split("|"):
          toks = excludesEntry.split(",")  
          excludes.append((int(toks[0]),toks[1],toks[2],int(toks[3]),int(toks[4])))
        excludes = list(set(excludes))
        for exclude in excludes:
          exToks.append((exclude[3],exclude[4]))
        exToks = list(set(exToks))
        exToks.sort(key=lambda tup: (tup[0], tup[1]))
        curOffset = startOffset
        for exTok in exToks:
          if exTok[0] <= curOffset:
            curOffset = exTok[1]
          else:
            text = text + docText[curOffset:exTok[0]]
            curOffset = exTok[1]
        if curOffset < endOffset:
          text = text + docText[curOffset:endOffset]
        
      else:
        text = docText[startOffset:endOffset]
    
      if properties != None:
        properties['text'] = text
      else:
        properties = {}
        properties['text'] = text
      return properties
  
  HydrateTextUDF = udf(HydrateText,MapType(StringType(),StringType()))

  hydratedf = df.sortWithinPartitions('docId') \
                .withColumn('properties', HydrateTextUDF(col('docId'),col('startOffset'),col('endOffset'),col('properties'),lit(txtPath),lit(excludes)))
  return hydratedf