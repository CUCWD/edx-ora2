# coding=utf-8
"""
These models have to capture not only the state of assessments made for certain
submissions, but also the state of the rubrics at the time those assessments
were made.

NOTE: We've switched to migrations, so if you make any edits to this file, you
need to then generate a matching migration for it using:

    ./manage.py schemamigration openassessment.assessment --auto

"""
from collections import defaultdict
from copy import deepcopy
from hashlib import sha1
import json

from django.db import models
from django.utils.timezone import now
from django.utils.translation import ugettext as _
import math


class InvalidOptionSelection(Exception):
    """
    The user selected options that do not match the rubric.
    """
    pass


class Rubric(models.Model):
    """A Rubric contains the guidelines on how to assess a submission.

    Rubrics are composed of :class:`Criterion` objects which are in turn
    composed of :class:`CriterionOption` objects.

    This model is a bit unusual in that it is the representation of the rubric
    that an assessment was made with *at the time of assessment*. The source
    rubric data lives in the problem definition, which is in the
    :class:`OpenAssessmentBlock`. When an assessment is made, the XBlock passes
    that rubric information along as well. When this Django app records the
    :class:`Assessment`, we do a lookup to see if the Rubric model object
    already exists (using hashing). If the Rubric is not found, we create a new
    one with the information OpenAssessmentBlock has passed in.

    .. warning::
       Never change Rubric model data after it's written!

    The little tree of objects that compose a Rubric is meant to be immutable —
    once created, they're never updated. When the problem changes, we end up
    creating a new Rubric instead. This makes it easy to cache and do hash-based
    lookups.
    """
    # SHA1 hash
    content_hash = models.CharField(max_length=40, unique=True, db_index=True)

    @property
    def points_possible(self):
        """The total number of points that could be earned in this Rubric."""
        criteria_points = [crit.points_possible for crit in self.criteria.all()]
        return sum(criteria_points) if criteria_points else 0

    @staticmethod
    def content_hash_from_dict(rubric_dict):
        """Given a dict of rubric information, return a unique hash.

        This is a static method because we want to provide the `content_hash`
        when we create the rubric -- i.e. before the Rubric object could know or
        access its child criteria or options. In Django, when you add child
        elements to a model object using a foreign key relation, it will
        immediately persist to the database. But in order to persist to the
        database, the child object needs to have the ID of the parent, meaning
        that Rubric would have to have already been created and persisted.
        """
        rubric_dict = deepcopy(rubric_dict)

        # Neither "id" nor "content_hash" would count towards calculating the
        # content_hash.
        rubric_dict.pop("id", None)
        rubric_dict.pop("content_hash", None)

        canonical_form = json.dumps(rubric_dict, sort_keys=True)
        return sha1(canonical_form).hexdigest()

    def options_ids(self, options_selected):
        """Given a mapping of selected options, return the option IDs.

        We use this to map user selection during assessment to the
        :class:`CriterionOption` IDs that are in our database. These IDs are
        never shown to the user.

        Args:
            options_selected (dict): Mapping of criteria names to the names of
                the option that was selected for that criterion.

        Returns:
            set of option ids

        Examples:
            >>> options_selected = {"secret": "yes", "safe": "no"}
            >>> rubric.options_ids(options_selected)
            [10, 12]

        Raises:
            InvalidOptionSelection: the selected options do not match the rubric.

        """
        # Select all criteria and options for this rubric
        # We use `select_related()` to minimize the number of database queries
        rubric_options = CriterionOption.objects.filter(criterion__rubric=self).select_related()

        # Create a dict of dicts that maps:
        # criterion names --> option names --> option ids
        rubric_criteria_dict = defaultdict(dict)

        # Construct dictionaries for each option in the rubric
        for option in rubric_options:
            rubric_criteria_dict[option.criterion.name][option.name] = option.id

        # Validate: are options selected for each criterion in the rubric?
        if len(options_selected) != len(rubric_criteria_dict):
            msg = _("Incorrect number of options for this rubric ({actual} instead of {expected}").format(
                actual=len(options_selected), expected=len(rubric_criteria_dict))
            raise InvalidOptionSelection(msg)

        # Look up each selected option
        option_id_set = set()
        for criterion_name, option_name in options_selected.iteritems():
            if (criterion_name in rubric_criteria_dict and
                option_name in rubric_criteria_dict[criterion_name]
            ):
                option_id = rubric_criteria_dict[criterion_name][option_name]
                option_id_set.add(option_id)
            else:
                msg = _("{criterion}: {option} not found in rubric").format(
                    criterion=criterion_name, option=option_name
                )
                raise InvalidOptionSelection(msg)

        return option_id_set


class Criterion(models.Model):
    """A single aspect of a submission that needs assessment.

    As an example, an essay might be assessed separately for accuracy, brevity,
    and clarity. Each of those would be separate criteria.

    All Rubrics have at least one Criterion.
    """
    rubric = models.ForeignKey(Rubric, related_name="criteria")

    name = models.CharField(max_length=100, blank=False)

    # 0-based order in the Rubric
    order_num = models.PositiveIntegerField()

    # What are we asking the reviewer to evaluate in this Criterion?
    prompt = models.TextField(max_length=10000)

    class Meta:
        ordering = ["rubric", "order_num"]

    @property
    def points_possible(self):
        """The total number of points that could be earned in this Criterion."""
        return max(option.points for option in self.options.all())


class CriterionOption(models.Model):
    """What an assessor chooses when assessing against a Criteria.

    CriterionOptions have a name, point value, and explanation associated with
    them. When you have to select between "Excellent", "Good", "Fair", "Bad" --
    those are options.

    Note that this is the representation of the choice itself, *not* a
    representation of a particular assessor's choice for a particular
    Assessment. That state is stored in :class:`AssessmentPart`.
    """
    # All Criteria must have at least one CriterionOption.
    criterion = models.ForeignKey(Criterion, related_name="options")

    # 0-based order in Criterion
    order_num = models.PositiveIntegerField()

    # How many points this option is worth. 0 is allowed.
    points = models.PositiveIntegerField()

    # Short name of the option. This is visible to the user.
    # Examples: "Excellent", "Good", "Fair", "Poor"
    name = models.CharField(max_length=100)

    # Longer text describing this option and why you should choose it.
    # Example: "The response makes 3-5 Monty Python references and at least one
    #           original Star Wars trilogy reference. Do not select this option
    #           if the author made any references to the second trilogy."
    explanation = models.TextField(max_length=10000, blank=True)

    class Meta:
        ordering = ["criterion", "order_num"]

    def __repr__(self):
        return (
            "CriterionOption(order_num={0.order_num}, points={0.points}, "
            "name={0.name!r}, explanation={0.explanation!r})"
        ).format(self)

    def __unicode__(self):
        return repr(self)


class Assessment(models.Model):
    """An evaluation made against a particular Submission and Rubric.

    This is student state information and is created when a student completes
    an assessment of some submission. It is composed of :class:`AssessmentPart`
    objects that map to each :class:`Criterion` in the :class:`Rubric` we're
    assessing against.
    """
    submission_uuid = models.CharField(max_length=128, db_index=True)
    rubric = models.ForeignKey(Rubric)

    scored_at = models.DateTimeField(default=now, db_index=True)
    scorer_id = models.CharField(max_length=40, db_index=True)
    score_type = models.CharField(max_length=2)

    feedback = models.TextField(max_length=10000, default="", blank=True)

    class Meta:
        ordering = ["-scored_at", "-id"]

    @property
    def points_earned(self):
        parts = [part.points_earned for part in self.parts.all()]
        return sum(parts) if parts else 0

    @property
    def points_possible(self):
        return self.rubric.points_possible

    def __unicode__(self):
        return u"Assessment {}".format(self.id)

    @classmethod
    def get_median_score_dict(cls, scores_dict):
        """Determine the median score in a dictionary of lists of scores

        For a dictionary of lists, where each list contains a set of scores,
        determine the median value in each list.

        Args:
            scores_dict (dict): A dictionary of lists of int values. These int
                values are reduced to a single value that represents the median.

        Returns:
            (dict): A dictionary with criterion name keys and median score
                values.

        Examples:
            >>> scores = {
            >>>     "foo": [1, 2, 3, 4, 5],
            >>>     "bar": [6, 7, 8, 9, 10]
            >>> }
            >>> Assessment.get_median_score_dict(scores)
            {"foo": 3, "bar": 8}

        """
        median_scores = {}
        for criterion, criterion_scores in scores_dict.iteritems():
            criterion_score = Assessment.get_median_score(criterion_scores)
            median_scores[criterion] = criterion_score
        return median_scores

    @staticmethod
    def get_median_score(scores):
        """Determine the median score in a list of scores

        Determine the median value in the list.

        Args:
            scores (list): A list of int values. These int values
                are reduced to a single value that represents the median.

        Returns:
            (int): The median score.

        Examples:
            >>> scores = 1, 2, 3, 4, 5]
            >>> Assessment.get_median_score(scores)
            3

        """
        total_criterion_scores = len(scores)
        sorted_scores = sorted(scores)
        median = int(math.ceil(total_criterion_scores / float(2)))
        if total_criterion_scores == 0:
            median_score = 0
        elif total_criterion_scores % 2:
            median_score = sorted_scores[median-1]
        else:
            median_score = int(
                math.ceil(
                    sum(sorted_scores[median-1:median+1])/float(2)
                )
            )
        return median_score

    @classmethod
    def scores_by_criterion(cls, submission_uuid, must_be_graded_by):
        """Create a dictionary of lists for scores associated with criterion

        Create a key value in a dict with a list of values, for every criterion
        found in an assessment.

        Iterate over every part of every assessment. Each part is associated with
        a criterion name, which becomes a key in the score dictionary, with a list
        of scores.

        Args:
            submission_uuid (str): Obtain assessments associated with this submission.
            must_be_graded_by (int): The number of assessments to include in this score analysis.

        Examples:
            >>> Assessment.scores_by_criterion('abcd', 3)
            {
                "foo": [1, 2, 3],
                "bar": [6, 7, 8]
            }
        """
        assessments = cls.objects.filter(submission_uuid=submission_uuid).order_by("scored_at")[:must_be_graded_by]

        scores = defaultdict(list)
        for assessment in assessments:
            for part in assessment.parts.all():
                criterion_name = part.option.criterion.name
                scores[criterion_name].append(part.option.points)
        return scores


class AssessmentPart(models.Model):
    """Part of an Assessment corresponding to a particular Criterion.

    This is student state -- `AssessmentPart` represents what the student
    assessed a submission with for a given `Criterion`. So an example would be::

      5 pts: "Excellent"

    It's implemented as a foreign key to the `CriterionOption` that was chosen
    by this assessor for this `Criterion`. So basically, think of this class
    as :class:`CriterionOption` + student state.
    """
    assessment = models.ForeignKey(Assessment, related_name='parts')

    # criterion = models.ForeignKey(Criterion) ?
    option = models.ForeignKey(CriterionOption) # TODO: no reverse

    @property
    def points_earned(self):
        return self.option.points

    @property
    def points_possible(self):
        return self.option.criterion.points_possible


class AssessmentFeedback(models.Model):
    """A response to a submission's feedback, judging accuracy or helpfulness."""
    submission_uuid = models.CharField(max_length=128, unique=True, db_index=True)
    assessments = models.ManyToManyField(Assessment, related_name='assessment_feedback', default=None)
    HELPFULNESS_CHOICES = (
        (0, 'These results were not at all helpful'),
        (1, 'These results were somewhat helpful'),
        (2, 'These results were helpful'),
        (3, 'These results were very helpful'),
        (4, 'These results were extremely helpful'),
    )
    helpfulness = models.IntegerField(choices=HELPFULNESS_CHOICES, default=2)
    feedback = models.TextField(max_length=10000, default="")


class PeerWorkflow(models.Model):
    """Internal Model for tracking Peer Assessment Workflow

    This model can be used to determine the following information required
    throughout the Peer Assessment Workflow:

    1) Get next submission that requires assessment.
    2) Does a submission have enough assessments?
    3) Has a student completed enough assessments?
    4) Does a student already have a submission open for assessment?
    5) Close open assessments when completed.
    6) Should 'over grading' be allowed for a submission?

    The student item is the author of the submission.  Peer Workflow Items are
    created for each assessment made by this student.

    """
    student_id = models.CharField(max_length=40, db_index=True)
    item_id = models.CharField(max_length=128, db_index=True)
    course_id = models.CharField(max_length=40, db_index=True)
    submission_uuid = models.CharField(max_length=128, db_index=True, unique=True)
    created_at = models.DateTimeField(default=now, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __repr__(self):
        return (
            "PeerWorkflow(student_id={0.student_id}, item_id={0.item_id}, "
            "course_id={0.course_id}, submission_uuid={0.submission_uuid})"
            "created_at={0.created_at}"
        ).format(self)

    def __unicode__(self):
        return repr(self)


class PeerWorkflowItem(models.Model):
    """Represents an assessment associated with a particular workflow

    Created every time a submission is requested for peer assessment. The
    associated workflow represents the scorer of the given submission, and the
    assessment represents the completed assessment for this work item.

    Assessments are represented as their ID, defaulting to -1. This is done to
    optimized complex queries against PeerWorkflowItems with the Assessments
    indexed, whereas a Null reference would be costly.

    """
    scorer_id = models.ForeignKey(PeerWorkflow, related_name='items')
    submission_uuid = models.CharField(max_length=128, db_index=True)
    started_at = models.DateTimeField(default=now, db_index=True)
    assessment = models.IntegerField(default=-1)

    class Meta:
        ordering = ["started_at", "id"]

    def __repr__(self):
        return (
            "PeerWorkflowItem(scorer_id={0.scorer_id}, "
            "submission_uuid={0.submission_uuid}, "
            "started_at={0.started_at}, assessment={0.assessment})"
        ).format(self)

    def __unicode__(self):
        return repr(self)